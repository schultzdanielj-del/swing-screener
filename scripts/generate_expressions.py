"""
Expression Generator — Reads setup-specific rules and produces concrete PCF expressions.

Usage:
    python scripts/generate_expressions.py dtss

Reads:  data/{setup}_expression_rules.json
Writes: data/{setup}_expressions.json

Each expression has:
  - name: unique identifier (e.g. "near_res_c_maxh20_atr14")
  - category: which rule it came from
  - description: human-readable
  - pcf_hint: what this would look like in TC2000 PCF (for reference)
  - compute: dict with operation type + params for the profiler to execute
"""

import json
import sys
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_near_resistance(rules):
    """Price close to prior highs — the double top forming."""
    exprs = []
    cfg = rules["near_resistance"]
    for maxh_p in cfg["maxh_periods"]:
        for price in cfg["price_refs"]:
            for norm in cfg["normalizers"]:
                p_lower = price.lower()
                name = f"near_res_{p_lower}_maxh{maxh_p}_{norm}"
                pcf = f"(MAXH{maxh_p} - {price}) / {norm.upper()}"
                exprs.append({
                    "name": name,
                    "category": "near_resistance",
                    "description": f"Distance from {price} to MAXH{maxh_p}, normalized by {norm}",
                    "pcf_hint": pcf,
                    "compute": {
                        "op": "distance_to_maxh",
                        "price_ref": price,
                        "maxh_period": maxh_p,
                        "normalizer": norm,
                    }
                })
    # Also: ratio C / MAXH (how close as percentage)
    for maxh_p in cfg["maxh_periods"]:
        name = f"near_res_ratio_maxh{maxh_p}"
        exprs.append({
            "name": name,
            "category": "near_resistance",
            "description": f"C / MAXH{maxh_p} ratio (1.0 = at high)",
            "pcf_hint": f"C / MAXH{maxh_p}",
            "compute": {"op": "ratio_c_maxh", "maxh_period": maxh_p}
        })
    return exprs


def generate_extension_above_mas(rules):
    """How far price is above key MAs — extension in ADR multiples."""
    exprs = []
    cfg = rules["extension_above_mas"]
    for ma in cfg["mas"]:
        for norm in cfg["normalizers"]:
            name = f"ext_{ma}_{norm}"
            ma_upper = ma.upper()
            pcf = f"(C - {ma_upper}) / {norm.upper()}"
            exprs.append({
                "name": name,
                "category": "extension_above_mas",
                "description": f"Extension: C above {ma_upper}, in {norm} multiples",
                "pcf_hint": pcf,
                "compute": {"op": "extension", "ma": ma, "normalizer": norm}
            })
    return exprs


def generate_ma_structure(rules):
    """MA slopes, spreads, stacking — confirming uptrend."""
    exprs = []
    cfg = rules["ma_structure"]

    # Slopes: (MA - MA.offset) / normalizer
    for ma in cfg["slope_mas"]:
        for offset in cfg["slope_offsets"]:
            for norm in cfg["slope_normalizers"]:
                name = f"slope_{ma}_off{offset}_{norm}"
                ma_upper = ma.upper()
                pcf = f"({ma_upper} - {ma_upper}.{offset}) / {norm.upper()}"
                exprs.append({
                    "name": name,
                    "category": "ma_structure",
                    "description": f"Slope of {ma_upper} over {offset} bars, in {norm}",
                    "pcf_hint": pcf,
                    "compute": {"op": "ma_slope", "ma": ma, "offset": offset, "normalizer": norm}
                })

    # Spreads: (MA_fast - MA_slow) / normalizer
    for fast, slow in cfg["spread_pairs"]:
        for norm in cfg["spread_normalizers"]:
            name = f"spread_{fast}_{slow}_{norm}"
            pcf = f"({fast.upper()} - {slow.upper()}) / {norm.upper()}"
            exprs.append({
                "name": name,
                "category": "ma_structure",
                "description": f"Spread: {fast.upper()} minus {slow.upper()}, in {norm}",
                "pcf_hint": pcf,
                "compute": {"op": "ma_spread", "ma_fast": fast, "ma_slow": slow, "normalizer": norm}
            })

    return exprs


def generate_momentum_stalling(rules):
    """ROC, RSI, volume ratios, ADX — momentum fading."""
    exprs = []
    cfg = rules["momentum_stalling"]

    # Rate of Change over N periods
    for p in cfg["roc_periods"]:
        name = f"roc_{p}"
        exprs.append({
            "name": name,
            "category": "momentum_stalling",
            "description": f"Rate of change over {p} bars (%)",
            "pcf_hint": f"100 * (C / C{p} - 1)",
            "compute": {"op": "roc", "period": p}
        })

    # ROC change (is ROC declining?) — ROC now vs ROC 5 bars ago
    for p in cfg["roc_periods"]:
        name = f"roc_delta_{p}"
        exprs.append({
            "name": name,
            "category": "momentum_stalling",
            "description": f"ROC{p} change vs 5 bars ago (stalling = negative)",
            "pcf_hint": f"(C/C{p} - 1) - (C5/C{p+5} - 1)",
            "compute": {"op": "roc_delta", "period": p, "compare_offset": 5}
        })

    # RSI values
    for p in cfg["rsi_periods"]:
        name = f"rsi_{p}"
        exprs.append({
            "name": name,
            "category": "momentum_stalling",
            "description": f"RSI {p}-period",
            "pcf_hint": f"RSI{p}.1",
            "compute": {"op": "rsi", "period": p}
        })

    # RSI slope (declining = stalling)
    for p in cfg["rsi_periods"]:
        name = f"rsi_slope_{p}"
        exprs.append({
            "name": name,
            "category": "momentum_stalling",
            "description": f"RSI{p} change over 5 bars",
            "pcf_hint": f"RSI{p}.1 - RSI{p}.1.5",
            "compute": {"op": "rsi_slope", "period": p, "offset": 5}
        })

    # Volume ratios
    for p in cfg["volume_avg_periods"]:
        name = f"vol_ratio_{p}"
        exprs.append({
            "name": name,
            "category": "momentum_stalling",
            "description": f"Volume / AVGV{p} ratio",
            "pcf_hint": f"V / AVGV{p}",
            "compute": {"op": "volume_ratio", "avg_period": p}
        })

    # ADX value and slope
    exprs.append({
        "name": "adx_14",
        "category": "momentum_stalling",
        "description": "ADX 14-period (trend strength)",
        "pcf_hint": "ADX14.14",
        "compute": {"op": "adx", "period": 14}
    })
    exprs.append({
        "name": "adx_14_slope",
        "category": "momentum_stalling",
        "description": "ADX 14 slope over 5 bars (rolling = declining)",
        "pcf_hint": "ADX14.14 - ADX14.14.5",
        "compute": {"op": "adx_slope", "period": 14, "offset": 5}
    })

    # DI+ minus DI- (directional conviction)
    exprs.append({
        "name": "di_spread",
        "category": "momentum_stalling",
        "description": "DI+ minus DI- (positive = bulls dominate)",
        "pcf_hint": "DIPLUS14 - DIMINUS14",
        "compute": {"op": "di_spread", "period": 14}
    })

    # Stochastic
    exprs.append({
        "name": "stoch_14",
        "category": "momentum_stalling",
        "description": "Stochastic %K 14-period",
        "pcf_hint": "STOC14.1",
        "compute": {"op": "stochastic", "period": 14}
    })

    # CCI
    exprs.append({
        "name": "cci_20",
        "category": "momentum_stalling",
        "description": "CCI 20-period",
        "pcf_hint": "CCI20",
        "compute": {"op": "cci", "period": 20}
    })

    # BOP (Balance of Power)
    exprs.append({
        "name": "bop_14",
        "category": "momentum_stalling",
        "description": "Balance of Power 14-period SMA",
        "pcf_hint": "BOP14",
        "compute": {"op": "bop", "period": 14}
    })

    return exprs


def generate_range_and_channel(rules):
    """Range position, pullback depth, channel slope."""
    exprs = []
    cfg = rules["range_and_channel"]

    # Range position: (C - MINL) / (MAXH - MINL)
    for p in cfg["range_periods"]:
        name = f"range_pos_{p}"
        exprs.append({
            "name": name,
            "category": "range_and_channel",
            "description": f"Position in {p}-bar range (0=low, 1=high)",
            "pcf_hint": f"(C - MINL{p}) / (MAXH{p} - MINL{p})",
            "compute": {"op": "range_position", "period": p}
        })

    # Pullback from high: (MAXH - C) / normalizer
    for p in cfg["pullback_periods"]:
        for norm in cfg["pullback_normalizers"]:
            name = f"pullback_{p}_{norm}"
            exprs.append({
                "name": name,
                "category": "range_and_channel",
                "description": f"Pullback from {p}-bar high, in {norm}",
                "pcf_hint": f"(MAXH{p} - C) / {norm.upper()}",
                "compute": {"op": "pullback", "period": p, "normalizer": norm}
            })

    # Range width (MAXH - MINL) / normalizer — volatility squeeze
    for p in cfg["range_periods"]:
        name = f"range_width_{p}_atr14"
        exprs.append({
            "name": name,
            "category": "range_and_channel",
            "description": f"Range width {p}-bar in ATR14 (squeeze = small)",
            "pcf_hint": f"(MAXH{p} - MINL{p}) / ATR14",
            "compute": {"op": "range_width", "period": p, "normalizer": "atr14"}
        })

    # Channel slope: (MAXH.0 - MAXH.offset) / normalizer
    for p in cfg["channel_slope_periods"]:
        name = f"channel_slope_highs_{p}"
        exprs.append({
            "name": name,
            "category": "range_and_channel",
            "description": f"Slope of {p}-bar highs (flat top = double top)",
            "pcf_hint": f"(MAXH{p} - MAXH{p}.{p}) / ATR14",
            "compute": {"op": "channel_slope", "ref": "maxh", "period": p, "normalizer": "atr14"}
        })

    # Candle size relative to ATR (small = indecision at top)
    exprs.append({
        "name": "candle_range_atr",
        "category": "range_and_channel",
        "description": "Today's range / ATR14 (small = indecision)",
        "pcf_hint": "(H - L) / ATR14",
        "compute": {"op": "candle_range_ratio"}
    })

    # Body size relative to range
    exprs.append({
        "name": "body_range_ratio",
        "category": "range_and_channel",
        "description": "Body / range ratio (small body = doji at top)",
        "pcf_hint": "ABS(C - O) / (H - L)",
        "compute": {"op": "body_range_ratio"}
    })

    # Upper wick ratio (rejection from above)
    exprs.append({
        "name": "upper_wick_ratio",
        "category": "range_and_channel",
        "description": "Upper wick / range (big = rejection)",
        "pcf_hint": "(H - GREATEST(C, O)) / (H - L)",
        "compute": {"op": "upper_wick_ratio"}
    })

    return exprs


def generate_extension_dynamics(rules):
    """Is the extension building or declining — slope of extension itself."""
    exprs = []
    cfg = rules["extension_dynamics"]

    for ma in cfg["ext_mas"]:
        for offset in cfg["ext_offsets"]:
            # Extension slope: ext_now - ext_N_bars_ago
            name = f"ext_slope_{ma}_off{offset}"
            ma_upper = ma.upper()
            exprs.append({
                "name": name,
                "category": "extension_dynamics",
                "description": f"Extension slope: (C-{ma_upper}) change over {offset} bars, in ADR14",
                "pcf_hint": f"((C - {ma_upper}) - (C{offset} - {ma_upper}.{offset})) / ADR14",
                "compute": {"op": "extension_slope", "ma": ma, "offset": offset, "normalizer": "adr14"}
            })

    # Max extension in recent window vs current (has it peaked?)
    for ma in cfg["ext_mas"]:
        name = f"ext_peak_ratio_{ma}"
        ma_upper = ma.upper()
        exprs.append({
            "name": name,
            "category": "extension_dynamics",
            "description": f"Current ext from {ma_upper} / max ext in 20 bars (< 1 = declining from peak)",
            "pcf_hint": f"(C - {ma_upper}) / MAX(C - {ma_upper}, 20)",
            "compute": {"op": "extension_peak_ratio", "ma": ma, "lookback": 20}
        })

    return exprs


def generate_boolean_patterns(rules):
    """CountTrue / SinceTrue / TrueInRow — pattern counting."""
    exprs = []
    cfg = rules["boolean_patterns"]

    for cond in cfg["conditions"]:
        # CountTrue over multiple periods
        for p in cfg["count_periods"]:
            name = f"ct_{cond}_{p}"
            exprs.append({
                "name": name,
                "category": "boolean_patterns",
                "description": f"CountTrue({cond}, {p})",
                "pcf_hint": f"CountTrue({cond}, {p})",
                "compute": {"op": "count_true", "condition": cond, "period": p}
            })

        # SinceTrue
        for p in cfg["since_periods"]:
            name = f"st_{cond}_{p}"
            exprs.append({
                "name": name,
                "category": "boolean_patterns",
                "description": f"SinceTrue({cond}, {p})",
                "pcf_hint": f"SinceTrue({cond}, {p})",
                "compute": {"op": "since_true", "condition": cond, "period": p}
            })

        # TrueInRow
        for p in cfg["tir_periods"]:
            name = f"tir_{cond}_{p}"
            exprs.append({
                "name": name,
                "category": "boolean_patterns",
                "description": f"TrueInRow({cond}, {p})",
                "pcf_hint": f"TrueInRow({cond}, {p})",
                "compute": {"op": "true_in_row", "condition": cond, "period": p}
            })

    return exprs


def generate_all(rules_path):
    """Read rules JSON, generate all expressions, return list."""
    with open(rules_path) as f:
        data = json.load(f)

    rules = data["rules"]

    all_exprs = []
    generators = [
        ("near_resistance", generate_near_resistance),
        ("extension_above_mas", generate_extension_above_mas),
        ("ma_structure", generate_ma_structure),
        ("momentum_stalling", generate_momentum_stalling),
        ("range_and_channel", generate_range_and_channel),
        ("extension_dynamics", generate_extension_dynamics),
        ("boolean_patterns", generate_boolean_patterns),
    ]

    for rule_name, gen_fn in generators:
        exprs = gen_fn(rules)
        all_exprs.extend(exprs)

    return all_exprs, data["setup_type"]


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/generate_expressions.py <setup_type>")
        sys.exit(1)

    setup_type = sys.argv[1]
    rules_path = os.path.join(REPO_ROOT, "data", f"{setup_type}_expression_rules.json")

    if not os.path.exists(rules_path):
        print(f"ERROR: {rules_path} not found")
        sys.exit(1)

    exprs, setup = generate_all(rules_path)

    # Count by category
    categories = {}
    for e in exprs:
        cat = e["category"]
        categories[cat] = categories.get(cat, 0) + 1

    # Save
    out_path = os.path.join(REPO_ROOT, "data", f"{setup_type}_expressions.json")
    with open(out_path, "w") as f:
        json.dump({
            "setup_type": setup,
            "total": len(exprs),
            "by_category": categories,
            "expressions": exprs,
        }, f, indent=2)

    # Print summary
    print(f"\n{'='*50}")
    print(f"  Expression Generator — {setup.upper()}")
    print(f"{'='*50}")
    print(f"\n  Total expressions: {len(exprs)}\n")
    for cat, count in categories.items():
        print(f"    {cat:30s} {count:4d}")
    print(f"\n  Saved to: {out_path}")

    # Estimate compute time
    n_bool = categories.get("boolean_patterns", 0)
    n_arith = len(exprs) - n_bool
    # Rough: bool ~0.5ms each, arith ~0.01ms each per ticker, 4167 tickers
    est_bool_s = n_bool * 0.5 / 1000 * 4167
    est_arith_s = n_arith * 0.01 / 1000 * 4167
    est_base_s = 25 / 1000 * 4167  # base indicator computation
    est_total = est_bool_s + est_arith_s + est_base_s
    print(f"\n  Estimated compute (4,167 tickers):")
    print(f"    Base indicators:  {est_base_s:6.0f}s")
    print(f"    Arithmetic ({n_arith:3d}):  {est_arith_s:6.1f}s")
    print(f"    Boolean ({n_bool:3d}):     {est_bool_s:6.0f}s")
    print(f"    Total:            {est_total:6.0f}s ({est_total/60:.1f} min)")
    print()


if __name__ == "__main__":
    main()
