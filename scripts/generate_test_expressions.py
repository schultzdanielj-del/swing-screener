"""
Generate a small test expression set for dry-run profiler testing.
~25 expressions, a few from each category.

Usage: python scripts/generate_test_expressions.py
"""

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

test_expressions = [
    # === NEAR RESISTANCE (3) ===
    {
        "name": "near_res_c_maxh20_atr14",
        "category": "near_resistance",
        "description": "Distance from C to MAXH20, in ATR14",
        "pcf_hint": "(MAXH20 - C) / ATR14",
        "compute": {"op": "distance_to_maxh", "price_ref": "C", "maxh_period": 20, "normalizer": "atr14"}
    },
    {
        "name": "near_res_c_maxh65_adr14",
        "category": "near_resistance",
        "description": "Distance from C to MAXH65, in ADR14",
        "pcf_hint": "(MAXH65 - C) / ADR14",
        "compute": {"op": "distance_to_maxh", "price_ref": "C", "maxh_period": 65, "normalizer": "adr14"}
    },
    {
        "name": "near_res_ratio_maxh65",
        "category": "near_resistance",
        "description": "C / MAXH65 ratio (1.0 = at high)",
        "pcf_hint": "C / MAXH65",
        "compute": {"op": "ratio_c_maxh", "maxh_period": 65}
    },

    # === EXTENSION ABOVE MAs (3) ===
    {
        "name": "ext_avgc50_adr14",
        "category": "extension_above_mas",
        "description": "Extension: C above AVGC50 in ADR14 multiples",
        "pcf_hint": "(C - AVGC50) / ADR14",
        "compute": {"op": "extension", "ma": "avgc50", "normalizer": "adr14"}
    },
    {
        "name": "ext_avgc200_adr14",
        "category": "extension_above_mas",
        "description": "Extension: C above AVGC200 in ADR14 multiples",
        "pcf_hint": "(C - AVGC200) / ADR14",
        "compute": {"op": "extension", "ma": "avgc200", "normalizer": "adr14"}
    },
    {
        "name": "ext_xavgc50_adr14",
        "category": "extension_above_mas",
        "description": "Extension: C above XAVGC50 in ADR14 multiples",
        "pcf_hint": "(C - XAVGC50) / ADR14",
        "compute": {"op": "extension", "ma": "xavgc50", "normalizer": "adr14"}
    },

    # === MA STRUCTURE (4) ===
    {
        "name": "slope_avgc50_off5_adr14",
        "category": "ma_structure",
        "description": "Slope of AVGC50 over 5 bars, in ADR14",
        "pcf_hint": "(AVGC50 - AVGC50.5) / ADR14",
        "compute": {"op": "ma_slope", "ma": "avgc50", "offset": 5, "normalizer": "adr14"}
    },
    {
        "name": "slope_xavgc8_off5_adr14",
        "category": "ma_structure",
        "description": "Slope of XAVGC8 over 5 bars, in ADR14",
        "pcf_hint": "(XAVGC8 - XAVGC8.5) / ADR14",
        "compute": {"op": "ma_slope", "ma": "xavgc8", "offset": 5, "normalizer": "adr14"}
    },
    {
        "name": "spread_xavgc8_xavgc21_adr14",
        "category": "ma_structure",
        "description": "Spread: XAVGC8 minus XAVGC21, in ADR14",
        "pcf_hint": "(XAVGC8 - XAVGC21) / ADR14",
        "compute": {"op": "ma_spread", "ma_fast": "xavgc8", "ma_slow": "xavgc21", "normalizer": "adr14"}
    },
    {
        "name": "spread_avgc50_avgc200_adr14",
        "category": "ma_structure",
        "description": "Spread: AVGC50 minus AVGC200, in ADR14",
        "pcf_hint": "(AVGC50 - AVGC200) / ADR14",
        "compute": {"op": "ma_spread", "ma_fast": "avgc50", "ma_slow": "avgc200", "normalizer": "adr14"}
    },

    # === MOMENTUM STALLING (5) ===
    {
        "name": "roc_10",
        "category": "momentum_stalling",
        "description": "Rate of change over 10 bars (%)",
        "pcf_hint": "100 * (C / C10 - 1)",
        "compute": {"op": "roc", "period": 10}
    },
    {
        "name": "roc_delta_10",
        "category": "momentum_stalling",
        "description": "ROC10 change vs 5 bars ago",
        "pcf_hint": "(C/C10 - 1) - (C5/C15 - 1)",
        "compute": {"op": "roc_delta", "period": 10, "compare_offset": 5}
    },
    {
        "name": "rsi_14",
        "category": "momentum_stalling",
        "description": "RSI 14-period",
        "pcf_hint": "RSI14.1",
        "compute": {"op": "rsi", "period": 14}
    },
    {
        "name": "vol_ratio_20",
        "category": "momentum_stalling",
        "description": "Volume / AVGV20 ratio",
        "pcf_hint": "V / AVGV20",
        "compute": {"op": "volume_ratio", "avg_period": 20}
    },
    {
        "name": "adx_14",
        "category": "momentum_stalling",
        "description": "ADX 14-period",
        "pcf_hint": "ADX14.14",
        "compute": {"op": "adx", "period": 14}
    },

    # === RANGE & CHANNEL (4) ===
    {
        "name": "range_pos_20",
        "category": "range_and_channel",
        "description": "Position in 20-bar range (0=low, 1=high)",
        "pcf_hint": "(C - MINL20) / (MAXH20 - MINL20)",
        "compute": {"op": "range_position", "period": 20}
    },
    {
        "name": "pullback_30_atr14",
        "category": "range_and_channel",
        "description": "Pullback from 30-bar high, in ATR14",
        "pcf_hint": "(MAXH30 - C) / ATR14",
        "compute": {"op": "pullback", "period": 30, "normalizer": "atr14"}
    },
    {
        "name": "candle_range_atr",
        "category": "range_and_channel",
        "description": "Today's range / ATR14",
        "pcf_hint": "(H - L) / ATR14",
        "compute": {"op": "candle_range_ratio"}
    },
    {
        "name": "upper_wick_ratio",
        "category": "range_and_channel",
        "description": "Upper wick / range (big = rejection)",
        "pcf_hint": "(H - GREATEST(C, O)) / (H - L)",
        "compute": {"op": "upper_wick_ratio"}
    },

    # === EXTENSION DYNAMICS (2) ===
    {
        "name": "ext_slope_avgc50_off5",
        "category": "extension_dynamics",
        "description": "Extension slope from AVGC50 over 5 bars",
        "pcf_hint": "((C - AVGC50) - (C5 - AVGC50.5)) / ADR14",
        "compute": {"op": "extension_slope", "ma": "avgc50", "offset": 5, "normalizer": "adr14"}
    },
    {
        "name": "ext_peak_ratio_avgc50",
        "category": "extension_dynamics",
        "description": "Current ext / max ext in 20 bars (< 1 = declining)",
        "pcf_hint": "(C - AVGC50) / MAX(C - AVGC50, 20)",
        "compute": {"op": "extension_peak_ratio", "ma": "avgc50", "lookback": 20}
    },

    # === BOOLEAN PATTERNS (4) ===
    {
        "name": "ct_c_gt_avgc50_20",
        "category": "boolean_patterns",
        "description": "CountTrue(C > AVGC50, 20)",
        "pcf_hint": "CountTrue(C > AVGC50, 20)",
        "compute": {"op": "count_true", "condition": "c_gt_avgc50", "period": 20}
    },
    {
        "name": "ct_c_gt_c1_10",
        "category": "boolean_patterns",
        "description": "CountTrue(C > C1, 10) — up days in last 10",
        "pcf_hint": "CountTrue(C > C1, 10)",
        "compute": {"op": "count_true", "condition": "c_gt_c1", "period": 10}
    },
    {
        "name": "st_rsi14_gt_70_20",
        "category": "boolean_patterns",
        "description": "SinceTrue(RSI14 > 70, 20) — bars since overbought",
        "pcf_hint": "SinceTrue(RSI14 > 70, 20)",
        "compute": {"op": "since_true", "condition": "rsi14_gt_70", "period": 20}
    },
    {
        "name": "ct_v_gt_avgv20_10",
        "category": "boolean_patterns",
        "description": "CountTrue(V > AVGV20, 10) — above avg vol days",
        "pcf_hint": "CountTrue(V > AVGV20, 10)",
        "compute": {"op": "count_true", "condition": "v_gt_avgv20", "period": 10}
    },
]

# Save
out_path = os.path.join(REPO_ROOT, "data", "dtss_expressions_test.json")

categories = {}
for e in test_expressions:
    cat = e["category"]
    categories[cat] = categories.get(cat, 0) + 1

with open(out_path, "w") as f:
    json.dump({
        "setup_type": "dtss",
        "total": len(test_expressions),
        "by_category": categories,
        "expressions": test_expressions,
    }, f, indent=2)

print(f"\nTest expression set: {len(test_expressions)} expressions")
for cat, count in categories.items():
    print(f"  {cat:30s} {count}")
print(f"\nSaved to: {out_path}")
