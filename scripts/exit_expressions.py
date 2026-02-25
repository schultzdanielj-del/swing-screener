"""
Post-Signal Exit Expression Library — ~4,000 expressions for exit grinder.

Every expression is evaluated at each forward bar relative to the signal bar.
The grinder tests every bar as a candidate exit and finds which expression
states correlate with maximum captured move.

Categories:
    move_captured, extension_from_ma, extension_dynamics, ma_reclaim,
    momentum_reversal, candle_character, volume_character, structural,
    range_compression, retracement, time, relative_strength

Each expression has:
    - name: unique identifier
    - category: grouping
    - compute: dict with 'op' and parameters (dispatched by exit_grinder)
"""


def generate_exit_expressions():
    """Generate the full post-signal exit expression library."""
    exprs = []

    # ═══════════════════════════════════════════════════════════
    # 1. MOVE CAPTURED — distance from entry to current bar
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

    # ═══════════════════════════════════════════════════════════
    # 2. EXTENSION FROM MA — where is price relative to MAs now?
    # ═══════════════════════════════════════════════════════════
    mas = ["xavgc8", "xavgc12", "xavgc21", "avgc50", "avgc200"]
    norms = ["adr14", "atr14"]
    for ma in mas:
        for norm in norms:
            exprs.append({
                "name": f"ext_{ma}_{norm}",
                "category": "extension_from_ma",
                "compute": {"op": "extension", "ma": ma, "normalizer": norm},
            })
            # Extension as ratio of historical ceiling for this ticker+MA
            for lookback in [252, 504, 1260]:
                exprs.append({
                    "name": f"ext_ceil_{ma}_{norm}_lb{lookback}",
                    "category": "extension_from_ma",
                    "compute": {"op": "extension_ceiling_ratio", "ma": ma,
                                "normalizer": norm, "lookback": lookback},
                })

    # ═══════════════════════════════════════════════════════════
    # 3. EXTENSION DYNAMICS — how is extension changing?
    # ═══════════════════════════════════════════════════════════
    for ma in mas:
        for norm in norms:
            for slope_lb in [1, 3, 5]:
                exprs.append({
                    "name": f"ext_slope_{ma}_{norm}_{slope_lb}b",
                    "category": "extension_dynamics",
                    "compute": {"op": "extension_slope", "ma": ma,
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
                "compute": {"op": "ext_acceleration", "ma": ma, "normalizer": norm},
            })

    # ═══════════════════════════════════════════════════════════
    # 4. MA RECLAIM — price crossing back above MAs
    # ═══════════════════════════════════════════════════════════
    for ma in mas:
        # Close above MA (boolean — for shorts, this means covering territory)
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
        # Distance from MA (signed)
        for norm in norms:
            exprs.append({
                "name": f"dist_from_{ma}_{norm}",
                "category": "ma_reclaim",
                "compute": {"op": "distance_from_ma", "ma": ma, "normalizer": norm},
            })
    # Sequential reclaim pairs
    for i, fast_ma in enumerate(mas[:-1]):
        for slow_ma in mas[i + 1:]:
            exprs.append({
                "name": f"reclaimed_{fast_ma}_and_{slow_ma}",
                "category": "ma_reclaim",
                "compute": {"op": "sequential_reclaim", "ma_fast": fast_ma, "ma_slow": slow_ma},
            })

    # ═══════════════════════════════════════════════════════════
    # 5. MOMENTUM REVERSAL — momentum turning against the move
    # ═══════════════════════════════════════════════════════════
    for p in [7, 14, 21]:
        exprs.append({"name": f"rsi_{p}", "category": "momentum_reversal",
                       "compute": {"op": "rsi", "period": p}})
        for sl in [3, 5]:
            exprs.append({"name": f"rsi_{p}_slope_{sl}", "category": "momentum_reversal",
                           "compute": {"op": "rsi_slope", "period": p, "offset": sl}})
    for roc_p in [1, 3, 5, 10]:
        exprs.append({"name": f"roc_{roc_p}", "category": "momentum_reversal",
                       "compute": {"op": "roc", "period": roc_p}})
    # MACD
    for fast, slow, sig in [(8, 17, 9), (12, 26, 9)]:
        exprs.append({"name": f"macd_hist_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_histogram", "fast": fast, "slow": slow, "signal": sig}})
        exprs.append({"name": f"macd_hist_slope_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_histogram_slope", "fast": fast, "slow": slow,
                                   "signal": sig, "offset": 3}})
    # Stochastic
    for p in [5, 14]:
        exprs.append({"name": f"stoch_{p}", "category": "momentum_reversal",
                       "compute": {"op": "stochastic", "period": p}})
    # ADX
    for p in [7, 14]:
        exprs.append({"name": f"adx_{p}", "category": "momentum_reversal",
                       "compute": {"op": "adx", "period": p}})
        exprs.append({"name": f"adx_{p}_slope_3", "category": "momentum_reversal",
                       "compute": {"op": "adx_slope", "period": p, "offset": 3}})
        exprs.append({"name": f"di_spread_{p}", "category": "momentum_reversal",
                       "compute": {"op": "di_spread", "period": p}})

    # ═══════════════════════════════════════════════════════════
    # 6. CANDLE CHARACTER — what does the current bar look like?
    # ═══════════════════════════════════════════════════════════
    exprs.append({"name": "bar_range_adr", "category": "candle_character",
                   "compute": {"op": "candle_range_ratio", "normalizer": "adr14"}})
    exprs.append({"name": "bar_range_atr", "category": "candle_character",
                   "compute": {"op": "candle_range_ratio", "normalizer": "atr14"}})
    exprs.append({"name": "body_range_ratio", "category": "candle_character",
                   "compute": {"op": "body_range_ratio"}})
    exprs.append({"name": "upper_wick_ratio", "category": "candle_character",
                   "compute": {"op": "upper_wick_ratio"}})
    exprs.append({"name": "lower_wick_ratio", "category": "candle_character",
                   "compute": {"op": "lower_wick_ratio"}})
    exprs.append({"name": "is_green", "category": "candle_character",
                   "compute": {"op": "is_green"}})
    for norm in ["adr14", "atr14"]:
        exprs.append({"name": f"gap_from_prior_{norm}", "category": "candle_character",
                       "compute": {"op": "gap_from_prior", "normalizer": norm}})
    # Rolling candle stats
    for window in [3, 5, 10]:
        exprs.append({"name": f"pct_green_{window}b", "category": "candle_character",
                       "compute": {"op": "pct_green_bars", "period": window}})
        exprs.append({"name": f"avg_body_ratio_{window}b", "category": "candle_character",
                       "compute": {"op": "avg_body_ratio", "period": window}})
        exprs.append({"name": f"avg_range_adr_{window}b", "category": "candle_character",
                       "compute": {"op": "avg_range_adr", "period": window}})

    # ═══════════════════════════════════════════════════════════
    # 7. VOLUME CHARACTER — volume behavior post-signal
    # ═══════════════════════════════════════════════════════════
    for avg_p in [20, 50]:
        exprs.append({"name": f"rvol_{avg_p}", "category": "volume_character",
                       "compute": {"op": "volume_ratio", "avg_period": avg_p}})
    for window in [3, 5, 10]:
        exprs.append({"name": f"avg_rvol_{window}b", "category": "volume_character",
                       "compute": {"op": "avg_rvol", "period": window, "avg_period": 20}})
        exprs.append({"name": f"up_vol_ratio_{window}b", "category": "volume_character",
                       "compute": {"op": "up_vol_ratio", "period": window}})
        exprs.append({"name": f"obv_slope_{window}b", "category": "volume_character",
                       "compute": {"op": "obv_slope_exit", "period": window}})
    # Volume relative to signal bar
    exprs.append({"name": "vol_vs_signal_bar", "category": "volume_character",
                   "compute": {"op": "vol_vs_signal_bar"}})

    # ═══════════════════════════════════════════════════════════
    # 8. STRUCTURAL — key levels reached
    # ═══════════════════════════════════════════════════════════
    for ma in ["avgc50", "avgc200"]:
        exprs.append({"name": f"touched_{ma}", "category": "structural",
                       "compute": {"op": "touched_ma", "ma": ma}})
        exprs.append({"name": f"closed_below_{ma}", "category": "structural",
                       "compute": {"op": "closed_below_ma", "ma": ma}})
    for p in [10, 20]:
        exprs.append({"name": f"new_low_count_{p}", "category": "structural",
                       "compute": {"op": "new_low_count", "period": p}})
        exprs.append({"name": f"higher_low_formed_{p}", "category": "structural",
                       "compute": {"op": "higher_low_formed", "period": p}})
    exprs.append({"name": "below_signal_low", "category": "structural",
                   "compute": {"op": "below_signal_low"}})

    # ═══════════════════════════════════════════════════════════
    # 9. RANGE COMPRESSION — move stalling out
    # ═══════════════════════════════════════════════════════════
    for window in [3, 5, 10]:
        exprs.append({"name": f"atr_ratio_vs_entry_{window}b", "category": "range_compression",
                       "compute": {"op": "atr_ratio", "period": 14, "offset": window}})
        exprs.append({"name": f"inside_bar_count_{window}b", "category": "range_compression",
                       "compute": {"op": "inside_bar_count", "period": window}})
    for p in [10, 20]:
        exprs.append({"name": f"bb_bandwidth_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_bandwidth", "period": p}})
        exprs.append({"name": f"bb_pctb_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_pctb", "period": p}})
        exprs.append({"name": f"bb_bw_rank_{p}_50", "category": "range_compression",
                       "compute": {"op": "bollinger_bandwidth_rank", "period": p, "lookback": 50}})

    # ═══════════════════════════════════════════════════════════
    # 10. RETRACEMENT — giving back the move
    # ═══════════════════════════════════════════════════════════
    for norm in ["adr14", "atr14", "pct"]:
        exprs.append({"name": f"retrace_from_mfe_{norm}", "category": "retracement",
                       "compute": {"op": "retrace_from_mfe", "normalizer": norm}})
    exprs.append({"name": "position_in_post_range", "category": "retracement",
                   "compute": {"op": "position_in_post_range"}})
    exprs.append({"name": "bars_since_mfe", "category": "retracement",
                   "compute": {"op": "bars_since_mfe"}})
    for n in [3, 5, 10]:
        exprs.append({"name": f"mfe_expanding_{n}b", "category": "retracement",
                       "compute": {"op": "mfe_expanding", "period": n}})

    # ═══════════════════════════════════════════════════════════
    # 11. TIME — bars since signal, velocity
    # ═══════════════════════════════════════════════════════════
    exprs.append({"name": "bars_since_signal", "category": "time",
                   "compute": {"op": "bars_since_signal"}})
    for norm in ["adr14", "atr14", "pct"]:
        exprs.append({"name": f"move_per_bar_{norm}", "category": "time",
                       "compute": {"op": "move_per_bar", "normalizer": norm}})

    # ═══════════════════════════════════════════════════════════
    # 12. RELATIVE STRENGTH — stock vs SPY
    # ═══════════════════════════════════════════════════════════
    for window in [5, 10, 20]:
        exprs.append({"name": f"rs_vs_spy_{window}", "category": "relative_strength",
                       "compute": {"op": "rs_vs_spy", "period": window}})

    return exprs


def generate_exit_boolean_conditions(base_exprs):
    """Identify boolean-eligible expressions and generate aggregated versions.

    Boolean conditions are expressions that can be evaluated as True/False
    at each bar. We aggregate them over rolling windows:
        count_true, pct_true, since_true, true_in_row
    """
    # Native boolean ops
    bool_ops = {
        "close_above_ma", "sequential_reclaim", "is_green",
        "touched_ma", "closed_below_ma", "below_signal_low",
        "higher_low_formed", "mfe_expanding",
    }

    # Threshold-based boolean versions of continuous expressions
    threshold_bools = []

    # RSI above 30/50/70
    for p in [7, 14, 21]:
        for thresh in [30, 50, 70]:
            threshold_bools.append({
                "name": f"rsi_{p}_above_{thresh}",
                "condition": {"base_op": "rsi", "period": p, "threshold": thresh, "direction": "above"},
            })

    # ROC positive
    for roc_p in [1, 3, 5, 10]:
        threshold_bools.append({
            "name": f"roc_{roc_p}_positive",
            "condition": {"base_op": "roc", "period": roc_p, "threshold": 0, "direction": "above"},
        })

    # Stochastic above 20/50/80
    for p in [5, 14]:
        for thresh in [20, 50, 80]:
            threshold_bools.append({
                "name": f"stoch_{p}_above_{thresh}",
                "condition": {"base_op": "stochastic", "period": p,
                              "threshold": thresh, "direction": "above"},
            })

    # ADX declining
    for p in [7, 14]:
        threshold_bools.append({
            "name": f"adx_{p}_declining",
            "condition": {"base_op": "adx_slope", "period": p, "offset": 3,
                          "threshold": 0, "direction": "below"},
        })

    # MACD histogram positive (momentum reversing for shorts)
    for fast, slow, sig in [(8, 17, 9), (12, 26, 9)]:
        threshold_bools.append({
            "name": f"macd_hist_{fast}_{slow}_{sig}_positive",
            "condition": {"base_op": "macd_histogram", "fast": fast, "slow": slow,
                          "signal": sig, "threshold": 0, "direction": "above"},
        })

    # Capture efficiency above thresholds
    for thresh in [0.5, 0.6, 0.7, 0.8, 0.9]:
        threshold_bools.append({
            "name": f"capture_eff_above_{int(thresh * 100)}",
            "condition": {"base_op": "capture_efficiency",
                          "threshold": thresh, "direction": "above"},
        })

    # Collect all boolean condition names
    native_bools = [e for e in base_exprs if e["compute"]["op"] in bool_ops]
    all_bool_names = [e["name"] for e in native_bools] + [tb["name"] for tb in threshold_bools]

    # Generate aggregated versions over windows
    agg_exprs = []
    windows = [5, 10, 15, 20, 30, 40, 60]
    agg_types = ["count_true", "pct_true", "since_true", "true_in_row"]

    for bool_name in all_bool_names:
        for agg in agg_types:
            for w in windows:
                agg_exprs.append({
                    "name": f"{bool_name}_{agg}_{w}b",
                    "category": "boolean",
                    "compute": {"op": f"bool_{agg}", "condition": bool_name, "period": w},
                })

    return threshold_bools, agg_exprs


def generate_all_exit_expressions():
    """Generate complete exit expression library with boolean aggregations."""
    base = generate_exit_expressions()
    threshold_bools, bool_aggs = generate_exit_boolean_conditions(base)

    # Summary
    cats = {}
    for e in base:
        c = e["category"]
        cats[c] = cats.get(c, 0) + 1

    return {
        "base_expressions": base,
        "threshold_booleans": threshold_bools,
        "boolean_aggregations": bool_aggs,
        "stats": {
            "base_count": len(base),
            "threshold_bool_count": len(threshold_bools),
            "bool_agg_count": len(bool_aggs),
            "total": len(base) + len(bool_aggs),
            "categories": cats,
        },
    }


if __name__ == "__main__":
    lib = generate_all_exit_expressions()
    stats = lib["stats"]
    print("Post-Signal Exit Expression Library")
    print("=" * 50)
    print(f"Base expressions: {stats['base_count']}")
    for cat, n in sorted(stats["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat:25s}: {n}")
    print(f"Threshold booleans: {stats['threshold_bool_count']}")
    print(f"Boolean aggregations: {stats['bool_agg_count']}")
    print(f"TOTAL: {stats['total']}")
