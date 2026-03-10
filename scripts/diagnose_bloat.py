"""
Grinder Bloat Diagnostic — Investigating why signals explode with more examples.

Compares grind results across 20/35/48/59/69 example counts to understand:
1. Range drift: how much do expression ranges widen per example added?
2. Condition set stability: are the same expressions being selected?
3. Dead weight: how many conditions have ranges so wide they're near-useless?
4. Tier cascade: which tier is contributing the most bloat?
5. The "perverse" effect: why removing outliers/junk makes it WORSE
"""

import json
import os
import numpy as np
from collections import Counter, defaultdict

GRINDS = [
    ("20ex", "grind_20ex.json", 264),
    ("35ex", "grind_35ex.json", 409),
    ("48ex", "grind_48ex.json", 489),
    ("59ex", "grind_59ex.json", 803),
    ("69ex", "grind_69ex.json", 1218),
]


def load_grind(path):
    with open(path) as f:
        return json.load(f)


def range_width(cond):
    return cond["high"] - cond["low"]


def analyze_range_drift():
    """Compare how expression ranges widen across grinds."""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 1: RANGE DRIFT ACROSS GRINDS")
    print("=" * 80)

    all_cond_sets = {}
    for label, path, _ in GRINDS:
        data = load_grind(path)
        conds = {c["name"]: c for c in data["all_conditions"]}
        all_cond_sets[label] = conds

    # Find expressions that appear in multiple grinds
    all_names = set()
    for conds in all_cond_sets.values():
        all_names.update(conds.keys())

    # Track which expressions appear across how many grinds
    expr_presence = {name: [] for name in all_names}
    for label, conds in all_cond_sets.items():
        for name in conds:
            expr_presence[name].append(label)

    # Expressions shared across grinds
    shared = {n: labels for n, labels in expr_presence.items() if len(labels) >= 2}
    print(f"\n  {len(all_names)} unique expressions used across all grinds")
    print(f"  {len(shared)} appear in 2+ grinds")

    # Count by how many grinds
    for n in range(5, 1, -1):
        count = sum(1 for labels in expr_presence.values() if len(labels) == n)
        if count > 0:
            print(f"  In {n}/5 grinds: {count} expressions")

    # For expressions in both 20ex and 69ex, show range widening
    print(f"\n  --- Range comparison: 20ex vs 69ex ---")
    print(f"  {'Expression':<45} {'20ex width':>12} {'69ex width':>12} {'Ratio':>8}")
    print(f"  {'-'*45} {'-'*12} {'-'*12} {'-'*8}")

    both = set(all_cond_sets["20ex"].keys()) & set(all_cond_sets["69ex"].keys())
    comparisons = []
    for name in sorted(both):
        c20 = all_cond_sets["20ex"][name]
        c69 = all_cond_sets["69ex"][name]
        w20 = range_width(c20)
        w69 = range_width(c69)
        ratio = w69 / w20 if w20 > 0 else float("inf")
        comparisons.append((name, w20, w69, ratio))

    comparisons.sort(key=lambda x: -x[3])
    for name, w20, w69, ratio in comparisons:
        print(f"  {name:<45} {w20:>12.4f} {w69:>12.4f} {ratio:>7.1f}x")

    if not both:
        print("  (No overlapping expressions between 20ex and 69ex)")


def analyze_condition_stability():
    """How much does the selected condition set change between grinds?"""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 2: CONDITION SET STABILITY")
    print("=" * 80)

    grind_sets = {}
    for label, path, _ in GRINDS:
        data = load_grind(path)
        names = set(c["name"] for c in data["all_conditions"])
        grind_sets[label] = names

    # Pairwise Jaccard similarity
    labels = [l for l, _, _ in GRINDS]
    print(f"\n  Jaccard similarity (|intersection| / |union|):")
    print(f"  {'':>8}", end="")
    for l in labels:
        print(f"  {l:>8}", end="")
    print()
    for l1 in labels:
        print(f"  {l1:>8}", end="")
        for l2 in labels:
            inter = len(grind_sets[l1] & grind_sets[l2])
            union = len(grind_sets[l1] | grind_sets[l2])
            jaccard = inter / union if union > 0 else 0
            print(f"  {jaccard:>7.1%}", end="")
        print()

    # What percentage of the 20ex conditions survive in later grinds?
    base = grind_sets["20ex"]
    print(f"\n  Survival of 20ex conditions ({len(base)} conditions) in later grinds:")
    for label in labels[1:]:
        surviving = base & grind_sets[label]
        print(f"    {label}: {len(surviving)}/{len(base)} survive "
              f"({len(surviving)/len(base):.0%})")


def analyze_dead_weight():
    """How many conditions have ranges so wide they're nearly useless?"""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 3: DEAD WEIGHT CONDITIONS")
    print("=" * 80)

    for label, path, signals in GRINDS:
        data = load_grind(path)
        conds = data["all_conditions"]

        # For historical tier conditions, we have filter_power
        # filter_power = (signals_without - signals_with_all) / signals_with_all
        # Low filter_power = removing this condition barely changes signal count
        hist_conds = [c for c in conds if c.get("filter_power") is not None]
        d1_conds = [c for c in conds if c.get("tier") == "D1"]

        if hist_conds:
            fps = [c["filter_power"] for c in hist_conds]
            weak = [c for c in hist_conds if c["filter_power"] < 0.05]
            dead = [c for c in hist_conds if c["filter_power"] < 0.01]
            print(f"\n  {label} ({signals} signals, {len(conds)} conditions):")
            print(f"    Historical conditions with filter_power: {len(hist_conds)}")
            print(f"    filter_power < 5% (weak): {len(weak)} "
                  f"({len(weak)/len(hist_conds):.0%})")
            print(f"    filter_power < 1% (dead): {len(dead)} "
                  f"({len(dead)/len(hist_conds):.0%})")
            if dead:
                for c in sorted(dead, key=lambda x: x["filter_power"]):
                    print(f"      {c['name']:<45} fp={c['filter_power']:.4f} "
                          f"tier={c['tier']} range=[{c['low']:.2f}, {c['high']:.2f}]")
            print(f"    D1 conditions (no filter_power): {len(d1_conds)}")

            # Distribution of filter_power
            fp_arr = np.array(fps)
            print(f"    filter_power stats: "
                  f"min={fp_arr.min():.4f} med={np.median(fp_arr):.4f} "
                  f"max={fp_arr.max():.4f} mean={fp_arr.mean():.4f}")


def analyze_tier_contribution():
    """Which tier is adding the most signal bloat?"""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 4: TIER BREAKDOWN")
    print("=" * 80)

    for label, path, signals in GRINDS:
        data = load_grind(path)
        conds = data["all_conditions"]
        tier_results = data.get("tier_results", {})

        tier_counts = Counter(c.get("tier", "?") for c in conds)
        print(f"\n  {label} ({signals} signals, {len(conds)} conditions):")
        print(f"    Conditions by tier: {dict(sorted(tier_counts.items()))}")

        # Check tier_results for signal progression
        # In multi-pass mode, keys are like "daily_D1", "daily_1wk", etc.
        tier_signals = {}
        for key, tr in tier_results.items():
            final = tr.get("final_total") or tr.get("n_passing")
            baseline = tr.get("baseline_peak")
            final_peak = tr.get("final_peak")
            if final:
                tier_signals[key] = {
                    "total": final,
                    "baseline_peak": baseline,
                    "final_peak": final_peak,
                    "conds_added": tr.get("conditions_added", 0),
                }

        if tier_signals:
            print(f"    Tier progression:")
            for key in sorted(tier_signals.keys()):
                ts = tier_signals[key]
                print(f"      {key:<20} total={ts['total']:>7,} "
                      f"peak: {ts.get('baseline_peak', '?')} → "
                      f"{ts.get('final_peak', '?')} "
                      f"(+{ts['conds_added']} conds)")


def analyze_category_drift():
    """How does the category mix change across example counts?"""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 5: CATEGORY MIX ACROSS GRINDS")
    print("=" * 80)

    all_cats = set()
    grind_cats = {}
    for label, path, signals in GRINDS:
        data = load_grind(path)
        cats = Counter(c.get("category", "?") for c in data["all_conditions"])
        grind_cats[label] = cats
        all_cats.update(cats.keys())

    all_cats = sorted(all_cats)
    labels = [l for l, _, _ in GRINDS]

    print(f"\n  {'Category':<30}", end="")
    for l in labels:
        print(f"  {l:>6}", end="")
    print()
    print(f"  {'-'*30}", end="")
    for _ in labels:
        print(f"  {'-'*6}", end="")
    print()

    for cat in all_cats:
        print(f"  {cat:<30}", end="")
        for l in labels:
            count = grind_cats[l].get(cat, 0)
            print(f"  {count:>6}", end="")
        print()

    # Total
    print(f"  {'TOTAL':<30}", end="")
    for l in labels:
        print(f"  {sum(grind_cats[l].values()):>6}", end="")
    print()


def analyze_boolean_range_explosion():
    """Boolean/count expressions should be integers. How wide are their ranges?"""
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 6: BOOLEAN/COUNT RANGE EXPLOSION")
    print("=" * 80)
    print("  Boolean conditions count occurrences (integers). Wide ranges = low filtering.")

    for label, path, signals in GRINDS:
        data = load_grind(path)
        bool_conds = [c for c in data["all_conditions"] if c.get("category") == "boolean"]
        if not bool_conds:
            continue

        print(f"\n  {label} ({len(bool_conds)} boolean conditions):")
        for c in sorted(bool_conds, key=lambda x: range_width(x), reverse=True):
            w = range_width(c)
            print(f"    {c['name']:<45} [{c['low']:>8.1f} — {c['high']:>8.1f}] "
                  f"width={w:>6.1f}  tier={c['tier']}")


def analyze_per_tier_signal_progression():
    """For the 69ex grind, trace how signals accumulate through tiers.

    This tells us: is the tier cascade compounding bad decisions?
    """
    print("\n" + "=" * 80)
    print("  DIAGNOSTIC 7: SIGNAL ACCUMULATION THROUGH 69ex TIER CASCADE")
    print("=" * 80)

    data = load_grind("grind_69ex.json")
    tier_results = data.get("tier_results", {})

    # The tier_results have final_signals for the last tier of each pass.
    # What we want is: after D1 alone, how many 5yr signals? After D1+1wk?
    # But that requires recomputation. What we CAN show is the tier_results
    # progression within each pass.

    print("\n  Tier results from the 69ex grind:")
    for key in sorted(tier_results.keys()):
        tr = tier_results[key]
        total = tr.get("final_total") or tr.get("n_passing") or 0
        peak = tr.get("final_peak") or tr.get("pass_rate")
        baseline_peak = tr.get("baseline_peak")
        conds = tr.get("conditions_added", 0)

        # Get condition names if available
        cond_names = tr.get("conditions", [])
        print(f"\n  {key}:")
        print(f"    conditions_added: {conds}")
        if baseline_peak is not None:
            print(f"    baseline_peak: {baseline_peak}")
        if peak is not None:
            print(f"    final_peak: {peak}")
        if total:
            print(f"    final_total: {total:,}")
        if cond_names:
            for cn in cond_names[:5]:
                print(f"      + {cn}")
            if len(cond_names) > 5:
                print(f"      ... and {len(cond_names) - 5} more")


def summary():
    """High-level summary table."""
    print("\n" + "=" * 80)
    print("  SUMMARY TABLE")
    print("=" * 80)
    print(f"\n  {'Grind':<8} {'Sigs':>6} {'Pk':>4} {'Conds':>6} {'Bool':>5} "
          f"{'ExtStr':>6} {'HTF':>5} {'Dead<1%':>8} {'Weak<5%':>8}")
    print(f"  {'-'*8} {'-'*6} {'-'*4} {'-'*6} {'-'*5} {'-'*6} {'-'*5} "
          f"{'-'*8} {'-'*8}")

    for label, path, signals in GRINDS:
        data = load_grind(path)
        conds = data["all_conditions"]
        n = len(conds)
        peak = data.get("summary", {}).get("final_peak", "?")

        booleans = sum(1 for c in conds if c.get("category") == "boolean")
        ext_str = sum(1 for c in conds if c.get("category") == "extension_structure")
        htf = sum(1 for c in conds
                  if c.get("category", "").startswith("htf_"))

        hist = [c for c in conds if c.get("filter_power") is not None]
        dead = sum(1 for c in hist if c["filter_power"] < 0.01)
        weak = sum(1 for c in hist if c["filter_power"] < 0.05)

        print(f"  {label:<8} {signals:>6} {peak:>4} {n:>6} {booleans:>5} "
              f"{ext_str:>6} {htf:>5} {dead:>8} {weak:>8}")


if __name__ == "__main__":
    summary()
    analyze_range_drift()
    analyze_condition_stability()
    analyze_dead_weight()
    analyze_tier_contribution()
    analyze_category_drift()
    analyze_boolean_range_explosion()
    analyze_per_tier_signal_progression()
