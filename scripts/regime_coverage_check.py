"""Quick diagnostic: why are ~38% of signals unscored by the regime model?"""
import os, sys, json
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def main():
    from scripts.market_grinder import (
        load_signals_from_refinement, find_latest_refinement,
        load_market_manifest, load_instrument_cache, deduplicate_features,
        compute_all_correlations, build_win_rate_series, WIN_CLASSES
    )

    setup = "dtss"
    path = find_latest_refinement(setup)
    print(f"Refinement: {os.path.basename(path)}")

    signals_df = load_signals_from_refinement(path, mode="pre")
    dates_str = signals_df["signal_date"].dt.strftime("%Y-%m-%d").values
    n_signals = len(signals_df)
    print(f"Signals: {n_signals}")

    manifest = load_market_manifest()

    # Build win rate series + correlations + top features (reuse the engine)
    print("Building correlations (this takes ~25s)...")
    wr_df = build_win_rate_series(signals_df, window=5)
    corr_df = compute_all_correlations(wr_df, manifest)
    top_df = deduplicate_features(corr_df, manifest, wr_df, 50)

    print(f"\nTop 50 features span {top_df['instrument'].nunique()} unique instruments")
    print(f"Instruments: {sorted(top_df['instrument'].unique())}\n")

    # For each feature, check how many signal dates have valid data
    print(f"{'Feature':<55} {'Dates w/ data':>13} {'Coverage':>9}")
    print("-" * 80)

    total_coverage = np.zeros(n_signals, dtype=int)

    for _, row in top_df.iterrows():
        inst_id = row["instrument"]
        expr_name = row["expr_name"]
        fname = row["feature_name"]

        dates_c, data_c = load_instrument_cache(inst_id)
        if dates_c is None:
            print(f"{fname:<55} {'NO CACHE':>13} {'0.0%':>9}")
            continue

        j = manifest["expr_names"].index(expr_name)
        date_set = set(dates_c)

        hits = 0
        for i, d in enumerate(dates_str):
            if d in date_set:
                idx = list(dates_c).index(d)
                if not np.isnan(data_c[idx, j]):
                    hits += 1
                    total_coverage[i] += 1

        pct = hits / n_signals * 100
        print(f"{fname:<55} {hits:>8}/{n_signals} {pct:>8.1f}%")

    # Summary: how many features does each signal have?
    print(f"\n{'='*60}")
    print(f"SIGNAL COVERAGE DISTRIBUTION")
    print(f"{'='*60}")
    for threshold in [0, 1, 5, 10, 20, 30, 40, 50]:
        n = int(np.sum(total_coverage >= threshold))
        print(f"  Signals with >= {threshold:>2} features: {n:>4}/{n_signals} ({n/n_signals*100:.1f}%)")

    # Show which date ranges are underserved
    print(f"\n{'='*60}")
    print(f"COVERAGE BY YEAR")
    print(f"{'='*60}")
    for year in ["2021", "2022", "2023", "2024", "2025", "2026"]:
        mask = np.array([d.startswith(year) for d in dates_str])
        if mask.sum() == 0:
            continue
        year_cov = total_coverage[mask]
        scorable = int(np.sum(year_cov >= 5))
        print(f"  {year}: {mask.sum():>4} signals, {scorable:>4} scorable (>= 5 features), "
              f"median coverage: {np.median(year_cov):.0f} features")


if __name__ == "__main__":
    main()
