"""
Post-Signal Exit Expression Library Generator

Generates ~4,000 expressions evaluated at each forward bar relative to
the signal bar. Used by exit_grinder.py to find optimal exit conditions.

Every expression is measured relative to a reference bar (the signal/entry bar)
looking forward. The grinder tests every bar as a candidate exit.

Categories:
    move_captured      - distance from entry to current bar
    extension_from_ma  - price extension from key MAs
    extension_dynamics - how extension is changing
    ma_reclaim         - price crossing back above/below MAs
    momentum_reversal  - momentum turning against the move
    candle_character   - current bar shape + rolling candle stats
    volume_character   - volume behavior post-signal
    structural         - key levels reached
    range_compression  - move stalling out
    retracement        - giving back the move
    time               - bars since signal, velocity
    relative_strength  - stock vs SPY (if available)
"""


# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════

MAS = ["xavgc8", "xavgc12", "xavgc21", "avgc50", "avgc200"]
NORMS = ["adr14", "atr14"]
FORWARD_WINDOWS = [5, 10, 15, 20, 30, 40, 60]
RSI_PERIODS = [7, 14, 21]
ROC_PERIODS = [1, 3, 5, 10]
SLOPE_LOOKBACKS = [1, 3, 5]
ROLLING_WINDOWS = [3, 5, 10]
STOCH_PERIODS = [5, 14]
ADX_PERIODS = [7, 14]
MACD_CONFIGS = [(8, 17, 9), (12, 26, 9)]
BB_PERIODS = [10, 20]
SWING_PERIODS = [10, 20]
VOL_AVG_PERIODS = [20, 50]


def generate_exit_expressions():
    """Generate the full post-signal exit expression library.
    
    Returns list of expression dicts, each with:
        name: unique identifier
        category: expression category
        compute: dict with 'op' and parameters for compute_exit_series()
    """
    exprs = []
    
    # ═══════════════════════════════════════════════════════
    # 1. MOVE CAPTURED
    # ═══════════════════════════════════════════════════════
    for price_ref in ["close", "low"]:
        for norm in NORMS:
            exprs.append({
                "name": f"move_captured_{price_ref}_{norm}",
                "category": "move_captured",
                "compute": {"op": "move_captured", "price_ref": price_ref, "normalizer": norm}
            })
            exprs.append({
                "name": f"mfe_{price_ref}_{norm}",
                "category": "move_captured",
                "compute": {"op": "mfe", "price_ref": price_ref, "normalizer": norm}
            })
    
    exprs.append({
        "name": "capture_efficiency",
        "category": "move_captured",
        "compute": {"op": "capture_efficiency"}
    })
    exprs.append({
        "name": "move_pct_from_entry",
        "category": "move_captured",
        "compute": {"op": "move_pct"}
    })
    
    # ═══════════════════════════════════════════════════════
    # 2. EXTENSION FROM MA
    # ═══════════════════════════════════════════════════════
    for ma in MAS:
        for norm in NORMS:
            exprs.append({
                "name": f"ext_{ma}_{norm}",
                "category": "extension_from_ma",
                "compute": {"op": "extension", "ma": ma, "normalizer": norm}
            })
            for lb in [126, 252]:
                exprs.append({
                    "name": f"ext_ceiling_ratio_{ma}_{norm}_{lb}",
                    "category": "extension_from_ma",
                    "compute": {"op": "ext_ceiling_ratio", "ma": ma, "normalizer": norm, "lookback": lb}
                })

    # ═══════════════════════════════════════════════════════
    # 3. EXTENSION DYNAMICS
    # ═══════════════════════════════════════════════════════
    for ma in MAS:
        for norm in NORMS:
            for slb in SLOPE_LOOKBACKS:
                exprs.append({
                    "name": f"ext_slope_{ma}_{norm}_{slb}",
                    "category": "extension_dynamics",
                    "compute": {"op": "ext_slope", "ma": ma, "normalizer": norm, "offset": slb}
                })
            exprs.append({
                "name": f"ext_retrace_from_peak_{ma}_{norm}",
                "category": "extension_dynamics",
                "compute": {"op": "ext_retrace_from_peak", "ma": ma, "normalizer": norm}
            })
            exprs.append({
                "name": f"ext_accel_{ma}_{norm}",
                "category": "extension_dynamics",
                "compute": {"op": "ext_accel", "ma": ma, "normalizer": norm}
            })

    # ═══════════════════════════════════════════════════════
    # 4. MA RECLAIM
    # ═══════════════════════════════════════════════════════
    for ma in MAS:
        exprs.append({
            "name": f"close_above_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "close_above_ma", "ma": ma}
        })
        exprs.append({
            "name": f"bars_since_reclaim_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "bars_since_reclaim", "ma": ma}
        })
        exprs.append({
            "name": f"reclaim_then_lost_{ma}",
            "category": "ma_reclaim",
            "compute": {"op": "reclaim_then_lost", "ma": ma}
        })
        for norm in NORMS:
            exprs.append({
                "name": f"distance_from_{ma}_{norm}",
                "category": "ma_reclaim",
                "compute": {"op": "distance_from_ma", "ma": ma, "normalizer": norm}
            })
    
    # Sequential reclaim pairs
    for i, fast_ma in enumerate(MAS[:-1]):
        for slow_ma in MAS[i+1:]:
            exprs.append({
                "name": f"reclaimed_{fast_ma}_and_{slow_ma}",
                "category": "ma_reclaim",
                "compute": {"op": "sequential_reclaim", "ma_fast": fast_ma, "ma_slow": slow_ma, "mode": "both"}
            })
            exprs.append({
                "name": f"reclaimed_{fast_ma}_not_{slow_ma}",
                "category": "ma_reclaim",
                "compute": {"op": "sequential_reclaim", "ma_fast": fast_ma, "ma_slow": slow_ma, "mode": "fast_only"}
            })

    # ═══════════════════════════════════════════════════════
    # 5. MOMENTUM REVERSAL
    # ═══════════════════════════════════════════════════════
    for p in RSI_PERIODS:
        exprs.append({"name": f"rsi_{p}", "category": "momentum_reversal",
                       "compute": {"op": "rsi", "period": p}})
        for slb in [3, 5]:
            exprs.append({"name": f"rsi_{p}_slope_{slb}", "category": "momentum_reversal",
                           "compute": {"op": "rsi_slope", "period": p, "offset": slb}})
        for thresh in [30, 50, 70]:
            exprs.append({"name": f"rsi_{p}_above_{thresh}", "category": "momentum_reversal",
                           "compute": {"op": "rsi_above", "period": p, "threshold": thresh}})
    
    for p in ROC_PERIODS:
        exprs.append({"name": f"roc_{p}", "category": "momentum_reversal",
                       "compute": {"op": "roc", "period": p}})
    
    for fast, slow, sig in MACD_CONFIGS:
        exprs.append({"name": f"macd_hist_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_histogram", "fast": fast, "slow": slow, "signal": sig}})
        exprs.append({"name": f"macd_hist_slope_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_histogram_slope", "fast": fast, "slow": slow, "signal": sig}})
        exprs.append({"name": f"macd_hist_positive_{fast}_{slow}_{sig}", "category": "momentum_reversal",
                       "compute": {"op": "macd_hist_positive", "fast": fast, "slow": slow, "signal": sig}})
    
    for p in STOCH_PERIODS:
        exprs.append({"name": f"stoch_{p}", "category": "momentum_reversal",
                       "compute": {"op": "stochastic", "period": p}})
        for thresh in [20, 50, 80]:
            exprs.append({"name": f"stoch_{p}_above_{thresh}", "category": "momentum_reversal",
                           "compute": {"op": "stoch_above", "period": p, "threshold": thresh}})
    
    for p in ADX_PERIODS:
        exprs.append({"name": f"adx_{p}", "category": "momentum_reversal",
                       "compute": {"op": "adx", "period": p}})
        exprs.append({"name": f"adx_{p}_slope_3", "category": "momentum_reversal",
                       "compute": {"op": "adx_slope", "period": p, "offset": 3}})
        exprs.append({"name": f"adx_{p}_declining", "category": "momentum_reversal",
                       "compute": {"op": "adx_declining", "period": p}})
        exprs.append({"name": f"di_spread_{p}", "category": "momentum_reversal",
                       "compute": {"op": "di_spread", "period": p}})

    # ═══════════════════════════════════════════════════════
    # 6. CANDLE CHARACTER
    # ═══════════════════════════════════════════════════════
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
    for norm in NORMS:
        exprs.append({"name": f"gap_from_prior_{norm}", "category": "candle_character",
                       "compute": {"op": "gap_from_prior", "normalizer": norm}})
    
    for w in ROLLING_WINDOWS:
        exprs.append({"name": f"pct_green_last_{w}", "category": "candle_character",
                       "compute": {"op": "pct_green_rolling", "window": w}})
        exprs.append({"name": f"avg_body_ratio_last_{w}", "category": "candle_character",
                       "compute": {"op": "avg_body_ratio_rolling", "window": w}})
        for norm in NORMS:
            exprs.append({"name": f"avg_bar_range_{norm}_last_{w}", "category": "candle_character",
                           "compute": {"op": "avg_bar_range_rolling", "normalizer": norm, "window": w}})
    
    exprs.append({"name": "consecutive_green", "category": "candle_character",
                   "compute": {"op": "consecutive_green"}})
    exprs.append({"name": "consecutive_red", "category": "candle_character",
                   "compute": {"op": "consecutive_red"}})

    # ═══════════════════════════════════════════════════════
    # 7. VOLUME CHARACTER
    # ═══════════════════════════════════════════════════════
    for avg_p in VOL_AVG_PERIODS:
        exprs.append({"name": f"rvol_vs_{avg_p}", "category": "volume_character",
                       "compute": {"op": "rvol", "avg_period": avg_p}})
    
    for w in ROLLING_WINDOWS:
        exprs.append({"name": f"avg_rvol_last_{w}", "category": "volume_character",
                       "compute": {"op": "avg_rvol_rolling", "window": w, "avg_period": 20}})
        exprs.append({"name": f"up_vol_ratio_last_{w}", "category": "volume_character",
                       "compute": {"op": "up_vol_ratio_rolling", "window": w}})
        exprs.append({"name": f"down_vol_ratio_last_{w}", "category": "volume_character",
                       "compute": {"op": "down_vol_ratio_rolling", "window": w}})
        exprs.append({"name": f"vol_trend_last_{w}", "category": "volume_character",
                       "compute": {"op": "vol_trend_rolling", "window": w}})
        exprs.append({"name": f"obv_slope_{w}", "category": "volume_character",
                       "compute": {"op": "obv_slope", "window": w}})
    
    exprs.append({"name": "vol_vs_signal_bar", "category": "volume_character",
                   "compute": {"op": "vol_vs_signal_bar"}})
    exprs.append({"name": "vol_rank_post_signal", "category": "volume_character",
                   "compute": {"op": "vol_rank_post_signal"}})

    # ═══════════════════════════════════════════════════════
    # 8. STRUCTURAL
    # ═══════════════════════════════════════════════════════
    for ma in ["avgc50", "avgc200"]:
        exprs.append({"name": f"touched_{ma}", "category": "structural",
                       "compute": {"op": "touched_ma", "ma": ma}})
        exprs.append({"name": f"closed_below_{ma}", "category": "structural",
                       "compute": {"op": "closed_below_ma", "ma": ma}})
        exprs.append({"name": f"bars_since_touch_{ma}", "category": "structural",
                       "compute": {"op": "bars_since_touch_ma", "ma": ma}})
    
    for p in SWING_PERIODS:
        exprs.append({"name": f"new_low_count_{p}", "category": "structural",
                       "compute": {"op": "new_low_count", "period": p}})
        exprs.append({"name": f"new_high_count_{p}", "category": "structural",
                       "compute": {"op": "new_high_count", "period": p}})
    
    exprs.append({"name": "lower_low_sequence", "category": "structural",
                   "compute": {"op": "lower_low_sequence"}})
    exprs.append({"name": "higher_low_formed", "category": "structural",
                   "compute": {"op": "higher_low_formed"}})
    exprs.append({"name": "below_signal_bar_low", "category": "structural",
                   "compute": {"op": "below_signal_bar_low"}})

    # ═══════════════════════════════════════════════════════
    # 9. RANGE COMPRESSION
    # ═══════════════════════════════════════════════════════
    for w in ROLLING_WINDOWS:
        for norm in NORMS:
            exprs.append({"name": f"atr_ratio_vs_entry_{norm}_{w}", "category": "range_compression",
                           "compute": {"op": "atr_ratio_vs_entry", "normalizer": norm, "window": w}})
        exprs.append({"name": f"range_contracting_{w}", "category": "range_compression",
                       "compute": {"op": "range_contracting", "window": w}})
        exprs.append({"name": f"inside_bar_count_{w}", "category": "range_compression",
                       "compute": {"op": "inside_bar_count", "window": w}})
    
    for p in BB_PERIODS:
        exprs.append({"name": f"bb_bandwidth_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_bandwidth", "period": p}})
        exprs.append({"name": f"bb_pctb_{p}", "category": "range_compression",
                       "compute": {"op": "bollinger_pctb", "period": p}})
        exprs.append({"name": f"bb_bw_rank_{p}_50", "category": "range_compression",
                       "compute": {"op": "bollinger_bandwidth_rank", "period": p, "lookback": 50}})

    # ═══════════════════════════════════════════════════════
    # 10. RETRACEMENT
    # ═══════════════════════════════════════════════════════
    exprs.append({"name": "retrace_from_mfe_pct", "category": "retracement",
                   "compute": {"op": "retrace_from_mfe_pct"}})
    for norm in NORMS:
        exprs.append({"name": f"retrace_from_mfe_{norm}", "category": "retracement",
                       "compute": {"op": "retrace_from_mfe", "normalizer": norm}})
    exprs.append({"name": "position_in_post_range", "category": "retracement",
                   "compute": {"op": "position_in_post_range"}})
    exprs.append({"name": "bars_since_mfe", "category": "retracement",
                   "compute": {"op": "bars_since_mfe"}})
    for n in [3, 5, 10]:
        exprs.append({"name": f"mfe_expanding_{n}", "category": "retracement",
                       "compute": {"op": "mfe_expanding", "window": n}})

    # ═══════════════════════════════════════════════════════
    # 11. TIME
    # ═══════════════════════════════════════════════════════
    exprs.append({"name": "bars_since_signal", "category": "time",
                   "compute": {"op": "bars_since_signal"}})
    for norm in NORMS:
        exprs.append({"name": f"move_per_bar_{norm}", "category": "time",
                       "compute": {"op": "move_per_bar", "normalizer": norm}})
    exprs.append({"name": "velocity_increasing", "category": "time",
                   "compute": {"op": "velocity_change", "direction": "increasing"}})
    exprs.append({"name": "velocity_decreasing", "category": "time",
                   "compute": {"op": "velocity_change", "direction": "decreasing"}})

    # ═══════════════════════════════════════════════════════
    # 12. RELATIVE STRENGTH (vs SPY)
    # ═══════════════════════════════════════════════════════
    for w in [5, 10, 20]:
        exprs.append({"name": f"rs_vs_spy_{w}", "category": "relative_strength",
                       "compute": {"op": "rs_vs_spy", "window": w}})
        exprs.append({"name": f"rs_vs_spy_slope_{w}", "category": "relative_strength",
                       "compute": {"op": "rs_vs_spy_slope", "window": w}})

    return exprs


def generate_exit_boolean_conditions(base_exprs):
    """Identify boolean expressions from the base set for aggregation.
    
    Returns list of expression names that produce boolean (0/1) values,
    suitable for count_true, since_true, true_in_row, pct_true aggregations.
    """
    bool_ops = {
        "close_above_ma", "reclaim_then_lost", "sequential_reclaim",
        "rsi_above", "stoch_above", "macd_hist_positive", "adx_declining",
        "is_green", "is_doji", "touched_ma", "closed_below_ma",
        "range_contracting", "below_signal_bar_low",
        "lower_low_sequence", "higher_low_formed",
        "mfe_expanding", "velocity_change",
    }
    return [e for e in base_exprs if e["compute"]["op"] in bool_ops]


def generate_all_exit_expressions():
    """Generate complete exit expression library including boolean aggregations.
    
    Returns list of expression dicts.
    """
    base = generate_exit_expressions()
    bool_exprs = generate_exit_boolean_conditions(base)
    
    # Boolean aggregations across forward windows
    agg_ops = ["count_true", "since_true", "true_in_row", "pct_true"]
    bool_aggs = []
    
    for expr in bool_exprs:
        for agg in agg_ops:
            for window in FORWARD_WINDOWS:
                bool_aggs.append({
                    "name": f"{expr['name']}_{agg}_{window}",
                    "category": "boolean",
                    "compute": {
                        "op": f"bool_{agg}",
                        "base_op": expr["compute"],
                        "window": window,
                    }
                })
    
    all_exprs = base + bool_aggs
    return all_exprs


if __name__ == "__main__":
    exprs = generate_all_exit_expressions()
    base = generate_exit_expressions()
    bools = generate_exit_boolean_conditions(base)
    
    print(f"POST-SIGNAL EXIT EXPRESSION LIBRARY")
    print(f"{'=' * 55}")
    
    cats = {}
    for e in exprs:
        c = e["category"]
        cats[c] = cats.get(c, 0) + 1
    
    for c, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {c:25s}: {n:5d}")
    
    print(f"  {'─'*25}  {'─'*5}")
    print(f"  {'TOTAL':25s}: {len(exprs):5d}")
    print(f"\n  Base expressions: {len(base)}")
    print(f"  Boolean candidates: {len(bools)}")
    print(f"  Boolean aggregations: {len(exprs) - len(base)}")
