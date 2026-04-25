"""Apply the top-40 example-band filter to the FULL deduped non-example pool per setup.

Uses the feature bands from the existing presignal_feature_carve CSV (ex_min, ex_max
per feature, sorted by carve rate). Evaluates on the full deduped cluster pool
(not a 400-sample), reporting how many non-example clusters survive.

Dedupe per §5.2: consecutive-bar dedupe per ticker, rightmost wins, split at
example signal bars.
"""
from __future__ import annotations

import os, sys, json, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_TXT = os.path.join(WORKTREE, "research", "carve_full_pool.txt")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

PYRAMIDS = {
    "htf":    "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":     "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":   "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":   "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db":  "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
FADE = {"dtss", "3-4db"}
TOP_K_VALUES = [10, 20, 40]
MIN_EX_MEASURED = 10
MIN_NE_MEASURED_FRAC = 0.5


def load_pyramid(path):
    with open(path, 'r') as f:
        d = json.load(f)
    tr = d["tier_results"]
    last_key = None
    for k, v in tr.items():
        if isinstance(v, dict) and "final_signals" in v:
            last_key = k
    return tr[last_key]["final_signals"] if last_key else []


def get_examples(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)).fetchall()
    conn.close()
    return [{"ticker": t, "entry_date": d} for t, d in rows]


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def lookup_idx(df, date_str):
    ds = dates_as_str(df)
    m = np.where(ds == date_str)[0]
    return int(m[0]) if len(m) > 0 else -1


def dedupe_signals_per_spec(raw_signals, universe, example_bar_set):
    """Consecutive-bar dedupe per ticker, rightmost wins, split at example bars.

    Returns (example_clusters, nonexample_clusters) — each a list of
    {ticker, date, bar_idx, is_example} dicts for the rightmost bar of each cluster.
    """
    # Group by ticker with bar_idx
    by_ticker = defaultdict(list)
    for s in raw_signals:
        df = universe.get(s["ticker"])
        if df is None:
            continue
        idx = lookup_idx(df, s["date"])
        if idx < 0:
            continue
        by_ticker[s["ticker"]].append({"ticker": s["ticker"], "date": s["date"], "bar_idx": idx})

    example_clusters = []
    nonexample_clusters = []

    for ticker, sigs in by_ticker.items():
        sigs.sort(key=lambda x: x["bar_idx"])
        cluster = [sigs[0]]
        for cur in sigs[1:]:
            prev = cluster[-1]
            # Break if non-consecutive OR previous was an example bar
            if cur["bar_idx"] != prev["bar_idx"] + 1 or (ticker, prev["bar_idx"]) in example_bar_set:
                rightmost = cluster[-1]
                is_ex = (ticker, rightmost["bar_idx"]) in example_bar_set
                target = example_clusters if is_ex else nonexample_clusters
                target.append({**rightmost, "is_example": is_ex})
                cluster = [cur]
            else:
                cluster.append(cur)
        # Final cluster
        rightmost = cluster[-1]
        is_ex = (ticker, rightmost["bar_idx"]) in example_bar_set
        target = example_clusters if is_ex else nonexample_clusters
        target.append({**rightmost, "is_example": is_ex})

    return example_clusters, nonexample_clusters


def build_feature_matrix(clusters, universe, ec):
    """Return (n_clusters, n_expr) matrix of feature values at each cluster's rightmost bar."""
    n_expr = ec.n_expressions
    by_ticker = defaultdict(list)
    for i, c in enumerate(clusters):
        by_ticker[c["ticker"]].append((i, c["date"]))

    mat = np.full((len(clusters), n_expr), np.nan, dtype=np.float32)
    for ticker, entries in by_ticker.items():
        expr_dates, expr_data = ec.get_ticker(ticker)
        if expr_dates is None:
            continue
        ed_str = np.array([str(d)[:10] for d in expr_dates])
        for i, date in entries:
            em = np.where(ed_str == date)[0]
            if len(em) == 0:
                continue
            mat[i, :] = expr_data[int(em[0]), :]
    return mat


def main():
    print("Loading OHLCV cache...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)} tickers", flush=True)
    if len(universe) < 11000:
        sys.exit("FAIL: ticker count too low")

    ec = ExprSeriesCache()
    if not ec.is_valid():
        sys.exit("FAIL: expr cache invalid")
    n_expr = ec.n_expressions
    expr_names = ec.expr_names
    print(f"Expr cache: {n_expr} expressions", flush=True)

    lines = []
    lines.append("CARVE ON FULL DEDUPED POOL — top-K AND filter, bands from examples")

    for setup, jf in PYRAMIDS.items():
        print(f"\n=== {setup} ===", flush=True)
        raw_signals = load_pyramid(os.path.join(CACHE_DIR, jf))
        examples = get_examples(setup)

        # Build example bar set: for each example, signal_idx = entry_idx - 1
        example_bar_set = set()
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                continue
            eidx = lookup_idx(df, ex["entry_date"])
            if eidx <= 0:
                continue
            example_bar_set.add((ex["ticker"], eidx - 1))

        example_clusters, nonexample_clusters = dedupe_signals_per_spec(
            raw_signals, universe, example_bar_set
        )
        print(f"  raw_signals={len(raw_signals)}  deduped_ex={len(example_clusters)}  deduped_nonex={len(nonexample_clusters)}", flush=True)

        print(f"  building feature matrices...", flush=True)
        ex_mat = build_feature_matrix(example_clusters, universe, ec)
        ne_mat = build_feature_matrix(nonexample_clusters, universe, ec)

        # Example bands
        ex_non_nan = np.sum(~np.isnan(ex_mat), axis=0)
        with np.errstate(all='ignore'):
            ex_min = np.nanmin(ex_mat, axis=0)
            ex_max = np.nanmax(ex_mat, axis=0)

        # Coverage gate
        ne_valid = ~np.isnan(ne_mat)
        ne_in_band = (ne_mat >= ex_min[None, :]) & (ne_mat <= ex_max[None, :]) & ne_valid
        ne_out_measured = (ne_valid & ~ne_in_band).sum(axis=0)
        ne_measured = ne_valid.sum(axis=0)
        with np.errstate(invalid='ignore', divide='ignore'):
            carve_value = np.where(ne_measured > 0, ne_out_measured / ne_measured, 0.0)
        ne_measured_frac = ne_measured / max(ne_mat.shape[0], 1)

        covered = (ex_non_nan >= MIN_EX_MEASURED) & (ne_measured_frac >= MIN_NE_MEASURED_FRAC) & (ex_non_nan >= 1)
        score = np.where(covered, carve_value, -np.inf)
        order = np.argsort(-score)

        lines.append("")
        lines.append("=" * 90)
        lines.append(f"{setup.upper()}  examples={len(example_clusters)} (deduped) "
                     f"non_examples={len(nonexample_clusters)} (deduped)")
        lines.append(f"  features passing coverage: {int(np.sum(covered))}/{n_expr}")
        lines.append("=" * 90)

        for top_n in TOP_K_VALUES:
            top_cols = [c for c in order[:top_n] if covered[c]]
            if not top_cols:
                lines.append(f"  top-{top_n}: no covered features")
                continue
            top_cols_arr = np.array(top_cols)
            ne_in_all = np.all(
                (ne_mat[:, top_cols_arr] >= ex_min[top_cols_arr][None, :])
                & (ne_mat[:, top_cols_arr] <= ex_max[top_cols_arr][None, :])
                & ~np.isnan(ne_mat[:, top_cols_arr]),
                axis=1,
            )
            kept = int(ne_in_all.sum())
            total = len(nonexample_clusters)
            lines.append(f"  top-{top_n:3d} AND-filter: {kept}/{total} non-example clusters survive ({kept/total*100:.1f}%)")

        # Show top-10 individual carve rates on FULL pool for reference
        lines.append("  Top-10 features (ranked on FULL non-example pool):")
        for rank_i, ci in enumerate(order[:10]):
            if not covered[ci]:
                break
            lo, hi = float(ex_min[ci]), float(ex_max[ci])
            cv = float(carve_value[ci]) * 100
            lines.append(f"    {rank_i+1:>2}. {expr_names[ci]:<52} [{lo:+.3g}, {hi:+.3g}]  carve={cv:.1f}%  n_ex={int(ex_non_nan[ci])}")

    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote {OUT_TXT}", flush=True)


if __name__ == "__main__":
    main()
