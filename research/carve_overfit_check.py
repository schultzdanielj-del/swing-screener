"""Overfit protection battery for the feature-band carve methodology.

Tests:
  1. Non-example train/test split: rank top-K on train, evaluate survival on test.
     Honest carve = test survival rate.
  2. Permutation null: randomly relabel non-examples as "pseudo-examples," rerun
     ranking + evaluation. If real carve >> permuted carve, real signal. Tight
     if similar.
  3. Example LOO: for each top-K feature, hold out each example, rebuild band
     on remaining. Does held-out example land in new band? Count failures.
  4. Feature decorrelation: pairwise correlation of top-K feature values on
     the union (ex + non-ex). Cluster |r|>=0.9. Pick one representative per cluster.
     True independent-feature count.

Output numbers per setup: train vs test carve, permuted baseline, LOO failure
rate per top-K, # independent features among top-40.
"""
from __future__ import annotations

import os, sys, json, pickle, sqlite3, random
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_TXT = os.path.join(WORKTREE, "research", "carve_overfit_check.txt")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

PYRAMIDS = {
    "htf":    "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":     "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":   "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":   "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db":  "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
MIN_EX_MEASURED = 10
MIN_NE_MEASURED_FRAC = 0.5
TOP_K_VALUES = [10, 20, 40]
N_SPLITS = 5     # different random splits for train/test
N_PERMUTATIONS = 10
CORR_THRESHOLD = 0.9  # feature correlation for decorrelation


def load_pyramid(path):
    with open(path, 'r') as f:
        d = json.load(f)
    tr = d["tier_results"]
    for k, v in reversed(list(tr.items())):
        if isinstance(v, dict) and "final_signals" in v:
            return v["final_signals"]
    return []


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


def dedupe_split(raw_signals, universe, example_bar_set):
    by_ticker = defaultdict(list)
    for s in raw_signals:
        df = universe.get(s["ticker"])
        if df is None:
            continue
        idx = lookup_idx(df, s["date"])
        if idx < 0:
            continue
        by_ticker[s["ticker"]].append({"ticker": s["ticker"], "date": s["date"], "bar_idx": idx})

    ex_cl, ne_cl = [], []
    for ticker, sigs in by_ticker.items():
        sigs.sort(key=lambda x: x["bar_idx"])
        cluster = [sigs[0]]
        for cur in sigs[1:]:
            prev = cluster[-1]
            if cur["bar_idx"] != prev["bar_idx"] + 1 or (ticker, prev["bar_idx"]) in example_bar_set:
                rm = cluster[-1]
                is_ex = (ticker, rm["bar_idx"]) in example_bar_set
                (ex_cl if is_ex else ne_cl).append(rm)
                cluster = [cur]
            else:
                cluster.append(cur)
        rm = cluster[-1]
        is_ex = (ticker, rm["bar_idx"]) in example_bar_set
        (ex_cl if is_ex else ne_cl).append(rm)
    return ex_cl, ne_cl


def build_matrix(clusters, universe, ec):
    n_expr = ec.n_expressions
    by_ticker = defaultdict(list)
    for i, c in enumerate(clusters):
        by_ticker[c["ticker"]].append((i, c["date"]))
    mat = np.full((len(clusters), n_expr), np.nan, dtype=np.float32)
    for ticker, entries in by_ticker.items():
        expr_dates, expr_data = ec.get_ticker(ticker)
        if expr_dates is None:
            continue
        ed = np.array([str(d)[:10] for d in expr_dates])
        for i, date in entries:
            em = np.where(ed == date)[0]
            if len(em) == 0:
                continue
            mat[i, :] = expr_data[int(em[0]), :]
    return mat


def rank_features(ex_mat, ne_mat):
    """Return (order, ex_min, ex_max, covered, carve_value). Rank by carve_value desc."""
    ex_non_nan = np.sum(~np.isnan(ex_mat), axis=0)
    with np.errstate(all='ignore'):
        ex_min = np.nanmin(ex_mat, axis=0)
        ex_max = np.nanmax(ex_mat, axis=0)
    ne_valid = ~np.isnan(ne_mat)
    ne_in_band = (ne_mat >= ex_min[None, :]) & (ne_mat <= ex_max[None, :]) & ne_valid
    ne_out_measured = (ne_valid & ~ne_in_band).sum(axis=0)
    ne_measured = ne_valid.sum(axis=0)
    with np.errstate(invalid='ignore', divide='ignore'):
        carve_value = np.where(ne_measured > 0, ne_out_measured / ne_measured, 0.0)
    ne_measured_frac = ne_measured / max(ne_mat.shape[0], 1)
    covered = (ex_non_nan >= MIN_EX_MEASURED) & (ne_measured_frac >= MIN_NE_MEASURED_FRAC)
    score = np.where(covered, carve_value, -np.inf)
    order = np.argsort(-score)
    return order, ex_min, ex_max, covered, carve_value


def and_filter_survival(ne_mat, top_cols, ex_min, ex_max):
    if len(top_cols) == 0:
        return ne_mat.shape[0]
    cols = np.asarray(top_cols)
    pass_mask = np.all(
        (ne_mat[:, cols] >= ex_min[cols][None, :])
        & (ne_mat[:, cols] <= ex_max[cols][None, :])
        & ~np.isnan(ne_mat[:, cols]),
        axis=1,
    )
    return int(pass_mask.sum())


def decorrelate(top_cols, union_mat, threshold=CORR_THRESHOLD):
    """Greedy decorrelation: keep feature if |r| with all already-kept < threshold."""
    if len(top_cols) == 0:
        return []
    kept = []
    kept_vals = []
    for c in top_cols:
        v = union_mat[:, c]
        mask = ~np.isnan(v)
        if mask.sum() < 10:
            continue
        redundant = False
        for kv in kept_vals:
            both = mask & ~np.isnan(kv)
            if both.sum() < 10:
                continue
            r = np.corrcoef(v[both], kv[both])[0, 1]
            if np.isfinite(r) and abs(r) >= threshold:
                redundant = True
                break
        if not redundant:
            kept.append(c)
            kept_vals.append(v)
    return kept


def main():
    rng = np.random.default_rng(42)
    print("Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)}", flush=True)

    ec = ExprSeriesCache()
    if not ec.is_valid():
        sys.exit("FAIL: expr cache invalid")
    n_expr = ec.n_expressions
    expr_names = ec.expr_names
    print(f"Expr cache: {n_expr}", flush=True)

    lines = []
    lines.append("OVERFIT PROTECTION BATTERY")
    lines.append(f"N_SPLITS={N_SPLITS}  N_PERMUTATIONS={N_PERMUTATIONS}  CORR_THRESHOLD={CORR_THRESHOLD}")

    for setup, jf in PYRAMIDS.items():
        print(f"\n=== {setup} ===", flush=True)
        raw = load_pyramid(os.path.join(CACHE_DIR, jf))
        examples = get_examples(setup)
        example_bar_set = set()
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                continue
            eidx = lookup_idx(df, ex["entry_date"])
            if eidx <= 0:
                continue
            example_bar_set.add((ex["ticker"], eidx - 1))

        ex_cl, ne_cl = dedupe_split(raw, universe, example_bar_set)
        print(f"  deduped: ex={len(ex_cl)} ne={len(ne_cl)}", flush=True)
        ex_mat = build_matrix(ex_cl, universe, ec)
        ne_mat = build_matrix(ne_cl, universe, ec)

        lines.append("")
        lines.append("=" * 90)
        lines.append(f"{setup.upper()}  deduped: ex={len(ex_cl)}  ne={len(ne_cl)}")
        lines.append("=" * 90)

        # ── 1. Non-example train/test split ──
        lines.append("\n[1] Non-example train/test split (rank on train, measure on test)")
        lines.append(f"    {'top-K':<6} {'train surv%':<12} {'test surv%':<12} {'gap (overfit)':<15}")
        for top_n in TOP_K_VALUES:
            train_rates, test_rates = [], []
            for sp in range(N_SPLITS):
                n_ne = ne_mat.shape[0]
                perm = rng.permutation(n_ne)
                half = n_ne // 2
                tr_idx, te_idx = perm[:half], perm[half:]
                ne_tr, ne_te = ne_mat[tr_idx], ne_mat[te_idx]
                order_tr, mn, mx, cov, cv_tr = rank_features(ex_mat, ne_tr)
                top_cols = [c for c in order_tr[:top_n] if cov[c]]
                tr_kept = and_filter_survival(ne_tr, top_cols, mn, mx)
                te_kept = and_filter_survival(ne_te, top_cols, mn, mx)
                train_rates.append(tr_kept / max(len(tr_idx), 1))
                test_rates.append(te_kept / max(len(te_idx), 1))
            tr_mean = np.mean(train_rates) * 100
            te_mean = np.mean(test_rates) * 100
            gap = te_mean - tr_mean
            lines.append(f"    {top_n:<6} {tr_mean:>10.1f}%  {te_mean:>10.1f}%  {gap:>+8.1f} pp")

        # ── 2. Permutation null ──
        lines.append("\n[2] Permutation null (pseudo-examples from non-ex sample)")
        # Pick n_ex random non-examples to act as examples; measure carve against remainder
        real_order, real_mn, real_mx, real_cov, real_cv = rank_features(ex_mat, ne_mat)
        lines.append(f"    real rank top-40 (covered): {int(np.sum(real_cov))} features passed coverage")
        lines.append(f"    {'top-K':<6} {'real surv%':<12} {'perm mean surv%':<18} {'perm p90 surv%':<18}")
        n_ex_eff = ex_mat.shape[0]
        perm_surv_by_k = {k: [] for k in TOP_K_VALUES}
        for p in range(N_PERMUTATIONS):
            idx = rng.permutation(ne_mat.shape[0])
            pseudo_ex_idx = idx[:n_ex_eff]
            pseudo_ne_idx = idx[n_ex_eff:]
            pseudo_ex_mat = ne_mat[pseudo_ex_idx]
            pseudo_ne_mat = ne_mat[pseudo_ne_idx]
            order_p, mn_p, mx_p, cov_p, _ = rank_features(pseudo_ex_mat, pseudo_ne_mat)
            for top_n in TOP_K_VALUES:
                top_cols_p = [c for c in order_p[:top_n] if cov_p[c]]
                k = and_filter_survival(pseudo_ne_mat, top_cols_p, mn_p, mx_p)
                perm_surv_by_k[top_n].append(k / max(len(pseudo_ne_idx), 1))
        for top_n in TOP_K_VALUES:
            top_cols_real = [c for c in real_order[:top_n] if real_cov[c]]
            real_kept = and_filter_survival(ne_mat, top_cols_real, real_mn, real_mx)
            real_surv = real_kept / max(ne_mat.shape[0], 1) * 100
            perm_arr = np.array(perm_surv_by_k[top_n]) * 100
            pmean = float(np.mean(perm_arr))
            p90 = float(np.percentile(perm_arr, 90))
            lines.append(f"    {top_n:<6} {real_surv:>10.1f}%  {pmean:>14.1f}%     {p90:>14.1f}%")
        lines.append("    (if real surv% << perm mean/p90, real signal discriminates; if similar, overfit)")

        # ── 3. Example LOO band stability on top-40 ──
        lines.append("\n[3] Example LOO band stability on top-40 features")
        top40 = [c for c in real_order[:40] if real_cov[c]]
        if top40:
            loo_fail_counts = []
            for ci in top40:
                # For each example, rebuild band on others, check if this ex is in new band
                vals = ex_mat[:, ci]
                mask = ~np.isnan(vals)
                if mask.sum() < 2:
                    loo_fail_counts.append(np.nan)
                    continue
                valid_vals = vals[mask]
                failures = 0
                for j, v in enumerate(valid_vals):
                    others = np.concatenate([valid_vals[:j], valid_vals[j+1:]])
                    if v < np.min(others) or v > np.max(others):
                        failures += 1
                loo_fail_counts.append(failures / max(mask.sum(), 1))
            mean_fail = float(np.nanmean(loo_fail_counts))
            high_fail = sum(1 for f in loo_fail_counts if np.isfinite(f) and f > 0.1)
            lines.append(f"    mean LOO failure rate across top-40: {mean_fail*100:.1f}%")
            lines.append(f"    top-40 features with >10% LOO failure: {high_fail}/{len(top40)}")
            lines.append("    (high LOO failure = band is defined by a single outlier example; weak generalization)")

        # ── 4. Decorrelation of top-40 ──
        lines.append("\n[4] Decorrelation of top-40 features")
        union_mat = np.vstack([ex_mat, ne_mat])
        top40_list = [c for c in real_order[:40] if real_cov[c]]
        indep = decorrelate(top40_list, union_mat, CORR_THRESHOLD)
        lines.append(f"    top-40 -> {len(indep)} independent features (|r| < {CORR_THRESHOLD})")
        # Apply AND filter using only independent features
        real_kept_indep = and_filter_survival(ne_mat, indep, real_mn, real_mx)
        lines.append(f"    AND-filter with {len(indep)} independent features: {real_kept_indep}/{ne_mat.shape[0]} ({real_kept_indep/max(ne_mat.shape[0],1)*100:.1f}%)")
        lines.append("    Independent features:")
        for c in indep[:20]:
            lines.append(f"      {expr_names[c]:<52}  [{real_mn[c]:+.3g}, {real_mx[c]:+.3g}]")

    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote {OUT_TXT}", flush=True)


if __name__ == "__main__":
    main()
