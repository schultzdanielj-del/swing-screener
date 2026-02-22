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
    # BOOLEAN PATTERNS — CountTrue / SinceTrue / TrueInRow
    # Every condition × every period
    # ═══════════════════════════════════════════════════════
    bool_conditions = [
        "c_gt_xavgc8", "c_gt_xavgc21", "c_gt_xavgc50", "c_gt_xavgc100",
        "c_gt_avgc50", "c_gt_avgc200",
        "c_gt_c1", "c_lt_c1",
        "h_gt_h1", "l_lt_l1",
        "v_gt_avgv20", "v_gt_2x_avgv20",
        "c_gt_o",
        "xavgc8_gt_xavgc21", "xavgc50_gt_xavgc200", "avgc50_gt_avgc200",
        "avgc50_rising", "avgc200_rising", "xavgc50_rising",
        "h_gt_maxh5_1", "l_lt_minl5_1", "c_gt_maxc10_1",
        "range_gt_atr", "body_gt_half_range",
        "c_upper_half", "c_lower_half",
        "diplus_gt_diminus",
        "rsi14_gt_50", "rsi14_gt_70", "rsi14_lt_30",
        "adx14_gt_25",
        "c_gt_bbtop", "c_lt_bbbot",
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


def main():
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
