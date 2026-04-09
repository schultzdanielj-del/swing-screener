"""Quick diagnostic: why are ~38% of signals unscored by the regime model?

Uses the already-saved regime output file — no correlation recomputation needed.
No multiprocessing. Safe on Windows.
"""
import os, sys, json
import numpy as np
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")
MKT_DIR   = os.path.join(CACHE_DIR, "market_series")
MANIFEST  = os.path.join(MKT_DIR, "_manifest.json")


def main():
    # Find latest regime output
    import glob
    regime_files = glob.glob(os.path.join(CACHE_DIR, "regime_dtss_*.json"))
    if not regime_files:
        print("No regime output files found. Run market_grinder.py first.")
        return
    regime_files.sort()
    regime_path = regime_files[-1]
    print(f"Regime output: {os.path.basename(regime_path)}")

    with open(regime_path) as f:
        regime = json.load(f)

    # Get feature list and signal scores
    feature_weights = regime.get("feature_weights", {})
    signal_scores = regime.get("signal_scores", [])
    n_total = len(signal_scores)
    n_scored = sum(1 for s in signal_scores if s.get("regime_score") is not None)
    n_unscored = n_total - n_scored
    print(f"Features in model: {len(feature_weights)}")
    print(f"Signals: {n_total} total, {n_scored} scored, {n_unscored} unscored ({n_unscored/n_total*100:.1f}%)")

    # Scored vs unscored by year
    print(f"\n{'='*60}")
    print(f"SCORED vs UNSCORED BY YEAR")
    print(f"{'='*60}")
    year_stats = defaultdict(lambda: {"scored": 0, "unscored": 0})
    for s in signal_scores:
        year = s["signal_date"][:4]
        if s.get("regime_score") is not None:
            year_stats[year]["scored"] += 1
        else:
            year_stats[year]["unscored"] += 1

    for year in sorted(year_stats):
        st = year_stats[year]
        total = st["scored"] + st["unscored"]
        pct = st["scored"] / total * 100 if total > 0 else 0
        print(f"  {year}: {total:>4} signals, {st['scored']:>4} scored ({pct:.0f}%), {st['unscored']:>4} unscored")

    # Load market manifest
    with open(MANIFEST) as f:
        manifest = json.load(f)

    all_dates = [s["signal_date"] for s in signal_scores]

    # Per-feature coverage
    print(f"\n{'='*60}")
    print(f"PER-FEATURE COVERAGE ON SIGNAL DATES")
    print(f"{'='*60}")
    print(f"{'Feature':<55} {'Hits':>10} {'Coverage':>9}")
    print("-" * 77)

    per_signal_hits = np.zeros(n_total, dtype=int)

    from local_runner.market_cache_builder import instrument_filename

    for fname, fw in feature_weights.items():
        inst_id = fw["instrument"]
        expr_name = fw["expr_name"]

        path = os.path.join(MKT_DIR, instrument_filename(inst_id))
        if not os.path.exists(path):
            print(f"{fname:<55} {'NO CACHE':>10} {'0.0%':>9}")
            continue

        with np.load(path, allow_pickle=True) as npf:
            dates_c = npf["dates"]
            data_c = npf["data"]

        try:
            j = manifest["expr_names"].index(expr_name)
        except ValueError:
            print(f"{fname:<55} {'NO EXPR':>10} {'0.0%':>9}")
            continue

        date_to_idx = {d: idx for idx, d in enumerate(dates_c)}

        hits = 0
        for i, d in enumerate(all_dates):
            idx = date_to_idx.get(d)
            if idx is not None and not np.isnan(data_c[idx, j]):
                hits += 1
                per_signal_hits[i] += 1

        pct = hits / n_total * 100
        print(f"{fname:<55} {hits:>5}/{n_total} {pct:>8.1f}%")

    # Features per signal distribution
    print(f"\n{'='*60}")
    print(f"FEATURES AVAILABLE PER SIGNAL")
    print(f"{'='*60}")
    for threshold in [0, 1, 5, 10, 20, 30, 40, 50]:
        n = int(np.sum(per_signal_hits >= threshold))
        print(f"  Signals with >= {threshold:>2} features: {n:>4}/{n_total} ({n/n_total*100:.1f}%)")

    # Coverage by year
    print(f"\n{'='*60}")
    print(f"FEATURE COVERAGE BY YEAR")
    print(f"{'='*60}")
    for year in sorted(year_stats):
        mask = np.array([d.startswith(year) for d in all_dates])
        if mask.sum() == 0:
            continue
        year_hits = per_signal_hits[mask]
        scorable = int(np.sum(year_hits >= 5))
        total_yr = int(mask.sum())
        print(f"  {year}: {total_yr:>4} signals, {scorable:>4} scorable (>= 5 features), "
              f"median: {np.median(year_hits):.0f}, min: {np.min(year_hits)}, max: {np.max(year_hits)}")


if __name__ == "__main__":
    main()
