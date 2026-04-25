"""Pre-signal feature carve mining — exhaustive scan over the full expression cache.

For each of the ~16k expression-cache features, at the signal bar:
  1. Compute the [min, max] band over ALL examples (enforces 100% example pass).
  2. Count non-example signal-pool bars that fall OUTSIDE that band (or are NaN).
  3. Rank features by carve rate = out-of-band non-example fraction.

Examples with NaN on a feature disqualify that feature entirely (can't have 100%
pass if any example is invalid).

Plus chart-shape archetype clustering on z-scored close over pre-signal window.
"""
from __future__ import annotations

import os, sys, json, pickle, random, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_TXT = os.path.join(WORKTREE, "research", "presignal_feature_carve.txt")
OUT_CSV = os.path.join(WORKTREE, "research", "presignal_feature_carve.csv")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

PYRAMIDS = {
    "htf":    "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":     "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":   "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":   "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db":  "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
BREAKOUT = {"htf", "bf", "base"}
FADE = {"dtss", "3-4db"}
N_NONEXAMPLE = 400
EXAMPLE_PROXIMITY_BARS = 5
SHAPE_NORM_BARS = 30
K_CLUSTERS = 3
SEED = 42
TOP_K_REPORT = 40
MIN_EX_MEASURED = 10       # min examples with non-nan value to rank a feature
MIN_NE_MEASURED_FRAC = 0.5 # min fraction of non-examples with non-nan value


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
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?", (setup,)
    ).fetchall()
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


def build_feature_matrix(signals, universe, ec):
    """Build a (n_signals, n_expr) matrix by loading each ticker's full .npz once.

    Returns (matrix, meta, shape_vecs) — rows with any load failure are dropped.
    """
    n_expr = ec.n_expressions
    # Group by ticker
    by_ticker = defaultdict(list)
    for i, s in enumerate(signals):
        by_ticker[s["ticker"]].append((i, s["date"]))

    out_rows = np.full((len(signals), n_expr), np.nan, dtype=np.float32)
    meta = [None] * len(signals)
    shapes = [None] * len(signals)

    for ticker, entries in by_ticker.items():
        df = universe.get(ticker)
        if df is None or len(df) < SHAPE_NORM_BARS + 1:
            continue
        dates = dates_as_str(df)
        closes = df["close"].values

        # Load cache once per ticker
        expr_dates, expr_data = ec.get_ticker(ticker)
        if expr_dates is None:
            continue
        ed_str = np.array([str(d)[:10] for d in expr_dates])

        for orig_i, date in entries:
            idx = lookup_idx(df, date)
            if idx < 0 or idx < SHAPE_NORM_BARS - 1:
                continue
            em = np.where(ed_str == date)[0]
            if len(em) == 0:
                continue
            ei = int(em[0])
            out_rows[orig_i, :] = expr_data[ei, :]
            meta[orig_i] = {"ticker": ticker, "date": date}
            # Shape vector: z-scored close over last SHAPE_NORM_BARS bars
            c = closes[idx - SHAPE_NORM_BARS + 1:idx + 1]
            if len(c) == SHAPE_NORM_BARS:
                mu, sd = np.mean(c), np.std(c)
                if sd > 0 and np.isfinite(sd):
                    shapes[orig_i] = ((c - mu) / sd).astype(np.float32)

    # Drop rows without meta
    valid = np.array([m is not None for m in meta])
    out_rows = out_rows[valid]
    meta = [m for m in meta if m is not None]
    shapes = [s for s, v in zip(shapes, valid) if v]
    return out_rows, meta, shapes


def kmeans_simple(X, k, n_iter=50, seed=0):
    rng = np.random.default_rng(seed)
    idx = rng.choice(X.shape[0], size=k, replace=False)
    centers = X[idx].copy()
    for _ in range(n_iter):
        d = np.linalg.norm(X[:, None, :] - centers[None, :, :], axis=2)
        labels = np.argmin(d, axis=1)
        new_centers = np.array([X[labels == j].mean(axis=0) if np.any(labels == j) else centers[j]
                                for j in range(k)])
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    return labels, centers


def main():
    random.seed(SEED)
    np.random.seed(SEED)

    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"OHLCV: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)} tickers", flush=True)
    if len(universe) < 11000:
        print("FAIL: too few tickers"); sys.exit(1)

    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expr cache invalid"); sys.exit(1)
    expr_names = ec.expr_names
    n_expr = ec.n_expressions
    print(f"Expr cache: {n_expr} expressions", flush=True)

    lines = []
    lines.append(f"PRE-SIGNAL FEATURE CARVE — exhaustive over {n_expr} expressions")
    lines.append(f"Seed={SEED}, non-example sample={N_NONEXAMPLE}, proximity exclusion=±{EXAMPLE_PROXIMITY_BARS}")
    lines.append("Rule: for each feature, ex [min,max] band enforces 100% example pass; carve = non-ex out-of-band fraction.")

    csv_rows = []

    for setup, jf in PYRAMIDS.items():
        print(f"\n=== {setup} ===", flush=True)
        signals_raw = load_pyramid(os.path.join(CACHE_DIR, jf))
        examples = get_examples(setup)
        is_fade = setup in FADE
        klass = "FADE" if is_fade else "BREAKOUT"

        # Example signals (signal bar = entry_date - 1)
        example_sigs = []
        for ex in examples:
            t, ed = ex["ticker"], ex["entry_date"]
            df = universe.get(t)
            if df is None:
                continue
            eidx = lookup_idx(df, ed)
            if eidx <= 0:
                continue
            sig_date = dates_as_str(df)[eidx - 1]
            example_sigs.append({"ticker": t, "date": sig_date})

        # Exclude non-examples near any example
        example_bar_set = set()
        for es in example_sigs:
            t = es["ticker"]
            df = universe.get(t)
            if df is None:
                continue
            i = lookup_idx(df, es["date"])
            if i < 0:
                continue
            for k in range(-EXAMPLE_PROXIMITY_BARS, EXAMPLE_PROXIMITY_BARS + 1):
                example_bar_set.add((t, i + k))

        non_ex_candidates = []
        for s in signals_raw:
            df = universe.get(s["ticker"])
            if df is None:
                continue
            i = lookup_idx(df, s["date"])
            if i < 0:
                continue
            if (s["ticker"], i) in example_bar_set:
                continue
            non_ex_candidates.append(s)
        random.shuffle(non_ex_candidates)
        non_ex_sample = non_ex_candidates[:N_NONEXAMPLE]

        print(f"  examples={len(example_sigs)} non_ex_pool={len(non_ex_candidates)} sample={len(non_ex_sample)}", flush=True)

        print(f"  building example feature matrix...", flush=True)
        ex_mat, ex_meta, ex_shapes = build_feature_matrix(example_sigs, universe, ec)
        print(f"    ex_mat shape: {ex_mat.shape}", flush=True)
        print(f"  building non-example feature matrix...", flush=True)
        ne_mat, ne_meta, ne_shapes = build_feature_matrix(non_ex_sample, universe, ec)
        print(f"    ne_mat shape: {ne_mat.shape}", flush=True)

        if ex_mat.shape[0] == 0 or ne_mat.shape[0] == 0:
            lines.append(f"\n[{setup}] insufficient data, skipping")
            continue

        # Per-feature carve
        # A feature is valid if AT LEAST ONE example has a non-NaN value
        # (else there's no band to compute). Examples with NaN are treated as
        # passing (search-side NaN=pass per §10.4), so they don't disqualify
        # the feature — recent IPOs keep weekly/monthly features alive.
        ex_non_nan_count = np.sum(~np.isnan(ex_mat), axis=0)
        valid_cols = ex_non_nan_count >= 1
        print(f"  features valid (>=1 ex non-nan): {int(np.sum(valid_cols))}/{n_expr}", flush=True)

        with np.errstate(all='ignore'):
            ex_min = np.nanmin(ex_mat, axis=0)
            ex_max = np.nanmax(ex_mat, axis=0)

        # Non-example out-of-band, split into two metrics:
        #   carve_raw   = all-non-ex out-of-band (NaN counts as out-of-band)
        #   carve_value = among non-ex with non-nan values, out-of-band fraction
        #                 (pure VALUE discrimination — ignores NaN-filter effect)
        ne_valid = ~np.isnan(ne_mat)
        ne_in_band = (ne_mat >= ex_min[None, :]) & (ne_mat <= ex_max[None, :]) & ne_valid
        ne_out = ~ne_in_band  # NaN → True (out-of-band in raw)
        carve_raw = ne_out.mean(axis=0)
        ne_measured_count = ne_valid.sum(axis=0)
        ne_out_of_measured = (ne_valid & ~ne_in_band).sum(axis=0)  # non-nan and out-of-band
        with np.errstate(invalid='ignore', divide='ignore'):
            carve_value = np.where(ne_measured_count > 0, ne_out_of_measured / ne_measured_count, 0.0)
        # How many examples/non-examples were actually measured (non-nan) per feature
        ex_measured = ex_non_nan_count
        ne_measured_frac = ne_measured_count / max(ne_mat.shape[0], 1)

        # Coverage filter: need enough measured examples + enough non-ex coverage
        covered = (ex_measured >= MIN_EX_MEASURED) & (ne_measured_frac >= MIN_NE_MEASURED_FRAC) & valid_cols

        # Width (in relative terms) — range normalized by median |value| on examples
        # to get a sense of how tight the example band is.
        ex_median_abs = np.nanmedian(np.abs(ex_mat), axis=0)
        ex_range = ex_max - ex_min
        # Narrowness = 1 / (range / (median_abs + eps)); large narrowness = very tight
        with np.errstate(divide='ignore', invalid='ignore'):
            narrow = ex_median_abs / (ex_range + 1e-9)

        # Rank by carve_value among features with enough coverage
        score = np.where(covered, carve_value, -np.inf)
        order = np.argsort(-score)

        lines.append("")
        lines.append("=" * 110)
        lines.append(f"{setup.upper()} ({klass})  examples={ex_mat.shape[0]}  non_examples={ne_mat.shape[0]}  "
                     f"valid_any_nonNaN={int(np.sum(valid_cols))}  passed_coverage={int(np.sum(covered))}")
        lines.append(f"  (coverage gate: n_ex_measured>={MIN_EX_MEASURED}, ne_measured_frac>={MIN_NE_MEASURED_FRAC})")
        lines.append("=" * 110)
        lines.append(f"  {'#':<3} {'feature':<52} {'ex [min,max]':<28} {'carve%':<8} {'n_ex':<6} {'ne_cov%':<8}")
        lines.append("  " + "-" * 110)
        for rank_i, ci in enumerate(order[:TOP_K_REPORT]):
            if not covered[ci]:
                break
            lo, hi = float(ex_min[ci]), float(ex_max[ci])
            cv = float(carve_value[ci]) * 100
            nex = int(ex_measured[ci])
            nec = float(ne_measured_frac[ci]) * 100
            band = f"[{lo:+.3g}, {hi:+.3g}]"
            lines.append(f"  {rank_i+1:<3} {expr_names[ci]:<52} {band:<28} {cv:6.1f}%  {nex:5d}  {nec:6.1f}%")

        # Save all valid feature carve results to CSV
        for ci in range(n_expr):
            if not valid_cols[ci]:
                continue
            csv_rows.append({
                "setup": setup, "feature": expr_names[ci],
                "ex_min": float(ex_min[ci]), "ex_max": float(ex_max[ci]),
                "ex_range": float(ex_range[ci]),
                "carve_raw": float(carve_raw[ci]),
                "carve_value": float(carve_value[ci]),
                "covered": bool(covered[ci]),
                "n_ex_total": int(ex_mat.shape[0]),
                "n_ex_measured": int(ex_measured[ci]),
                "n_ne": int(ne_mat.shape[0]),
                "n_ne_measured": int(ne_measured_count[ci]),
                "n_ne_in_band": int(ne_in_band[:, ci].sum()),
            })

        # Combined carve: AND the top-K coverage-passing features' bands.
        # For each non-example: in-band = value present AND within [min,max] on ALL top features.
        for top_n in (1, 3, 5, 10, 20, 40):
            top_cols = [c for c in order[:top_n] if covered[c]]
            if not top_cols:
                continue
            top_cols = np.array(top_cols)
            ne_in_all = np.all(
                (ne_mat[:, top_cols] >= ex_min[top_cols][None, :])
                & (ne_mat[:, top_cols] <= ex_max[top_cols][None, :])
                & ~np.isnan(ne_mat[:, top_cols]),
                axis=1,
            )
            kept = int(ne_in_all.sum())
            lines.append(f"  Combined top-{top_n:2d} AND filter: {kept}/{ne_mat.shape[0]} non-examples pass ({kept/ne_mat.shape[0]*100:.1f}%)")

        # Archetype clustering on shape vectors
        ex_shapes_arr = np.array([s for s in ex_shapes if s is not None])
        ne_shapes_arr = np.array([s for s in ne_shapes if s is not None])
        if len(ex_shapes_arr) >= K_CLUSTERS and len(ne_shapes_arr) > 0:
            labels, centers = kmeans_simple(ex_shapes_arr, K_CLUSTERS, seed=SEED)
            lines.append("")
            lines.append(f"  Chart-shape archetypes (k={K_CLUSTERS}, {SHAPE_NORM_BARS}-bar z-scored close):")
            for ci_k in range(K_CLUSTERS):
                n_in = int(np.sum(labels == ci_k))
                step = max(1, SHAPE_NORM_BARS // 10)
                pts = centers[ci_k][::step]
                pts_str = "  ".join(f"{x:+.2f}" for x in pts)
                lines.append(f"    cluster {ci_k}: n={n_in}  centroid[::{step}]: {pts_str}")
            # Non-example distance to nearest centroid vs example in-cluster distance
            dmat_ne = np.linalg.norm(ne_shapes_arr[:, None, :] - centers[None, :, :], axis=2)
            nearest_ne = np.min(dmat_ne, axis=1)
            ex_self_d = np.array([np.linalg.norm(ex_shapes_arr[j] - centers[labels[j]]) for j in range(len(ex_shapes_arr))])
            ex_max_d = float(np.max(ex_self_d))  # 100% example coverage radius
            ne_in_hull = int(np.sum(nearest_ne <= ex_max_d))
            lines.append(f"    example-to-centroid max radius (100% coverage): {ex_max_d:.2f}")
            lines.append(f"    non-examples within that radius: {ne_in_hull}/{len(ne_shapes_arr)} ({ne_in_hull/len(ne_shapes_arr)*100:.1f}%)")

    # Write outputs
    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    pd.DataFrame(csv_rows).to_csv(OUT_CSV, index=False)
    print(f"\nWrote {len(lines)} lines to {OUT_TXT}", flush=True)
    print(f"Wrote {len(csv_rows)} rows to {OUT_CSV}", flush=True)


if __name__ == "__main__":
    main()
