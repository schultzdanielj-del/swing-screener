"""List the non-example deduped signals that survive the decorrelated top-K AND filter,
per setup. Applies the full presignal-grinder pipeline including overfit-aware
decorrelation, then emits the survivors list."""
from __future__ import annotations

import os, sys, json, pickle, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
OUT_TXT = os.path.join(WORKTREE, "research", "presignal_survivors.txt")

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
TOP_K = 40
CORR_THRESHOLD = 0.9
# Per-setup verdicts from overfit battery
VERDICTS = {
    "htf": "MARGINAL (LOO fragile, small n)",
    "bf": "REAL",
    "base": "REAL",
    "dtss": "FAILS permutation null — do not deploy",
    "3-4db": "REAL",
}


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


def rank(ex_mat, ne_mat):
    ex_non_nan = np.sum(~np.isnan(ex_mat), axis=0)
    with np.errstate(all='ignore'):
        ex_min = np.nanmin(ex_mat, axis=0)
        ex_max = np.nanmax(ex_mat, axis=0)
    ne_valid = ~np.isnan(ne_mat)
    ne_in_band = (ne_mat >= ex_min[None, :]) & (ne_mat <= ex_max[None, :]) & ne_valid
    ne_out_m = (ne_valid & ~ne_in_band).sum(axis=0)
    ne_m = ne_valid.sum(axis=0)
    with np.errstate(invalid='ignore', divide='ignore'):
        cv = np.where(ne_m > 0, ne_out_m / ne_m, 0.0)
    ne_m_frac = ne_m / max(ne_mat.shape[0], 1)
    covered = (ex_non_nan >= MIN_EX_MEASURED) & (ne_m_frac >= MIN_NE_MEASURED_FRAC)
    order = np.argsort(-np.where(covered, cv, -np.inf))
    return order, ex_min, ex_max, covered


def decorrelate(top_cols, union_mat, threshold=CORR_THRESHOLD):
    kept, kept_vals = [], []
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
    print("Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)}", flush=True)

    ec = ExprSeriesCache()
    if not ec.is_valid():
        sys.exit("FAIL: expr cache invalid")
    expr_names = ec.expr_names

    lines = []
    lines.append("PRESIGNAL SURVIVORS — non-example clusters passing decorrelated top-K AND filter")
    lines.append(f"Filter: top-{TOP_K} features by carve_value, decorrelated at |r|>={CORR_THRESHOLD}")

    for setup, jf in PYRAMIDS.items():
        print(f"\n=== {setup} ===", flush=True)
        raw = load_pyramid(os.path.join(CACHE_DIR, jf))
        examples = get_examples(setup)
        example_bar_set = set()
        ticker_has_example = set()
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                continue
            eidx = lookup_idx(df, ex["entry_date"])
            if eidx <= 0:
                continue
            example_bar_set.add((ex["ticker"], eidx - 1))
            ticker_has_example.add(ex["ticker"])

        ex_cl, ne_cl = dedupe_split(raw, universe, example_bar_set)
        ex_mat = build_matrix(ex_cl, universe, ec)
        ne_mat = build_matrix(ne_cl, universe, ec)

        order, ex_min, ex_max, covered = rank(ex_mat, ne_mat)
        top = [int(c) for c in order[:TOP_K] if covered[c]]
        union_mat = np.vstack([ex_mat, ne_mat])
        indep = decorrelate(top, union_mat, CORR_THRESHOLD)
        indep_arr = np.array(indep)

        # AND-filter on non-examples, both strict (NaN-as-fail) and lenient (NaN-as-pass)
        if len(indep_arr) == 0:
            pass_mask = np.zeros(ne_mat.shape[0], dtype=bool)
            pass_mask_lenient = np.zeros(ne_mat.shape[0], dtype=bool)
            ex_pass_mask = np.zeros(ex_mat.shape[0], dtype=bool)
            ex_pass_mask_nan_ok = np.zeros(ex_mat.shape[0], dtype=bool)
        else:
            # NON-EX strict: NaN-as-fail
            pass_mask = np.all(
                (ne_mat[:, indep_arr] >= ex_min[indep_arr][None, :])
                & (ne_mat[:, indep_arr] <= ex_max[indep_arr][None, :])
                & ~np.isnan(ne_mat[:, indep_arr]),
                axis=1,
            )
            # NON-EX lenient: NaN-as-pass (same policy that keeps 100% of examples)
            ne_val = ne_mat[:, indep_arr]
            pass_mask_lenient = np.all(
                np.isnan(ne_val)
                | ((ne_val >= ex_min[indep_arr][None, :]) & (ne_val <= ex_max[indep_arr][None, :])),
                axis=1,
            )
            # EX strict: NaN-as-fail
            ex_pass_mask = np.all(
                (ex_mat[:, indep_arr] >= ex_min[indep_arr][None, :])
                & (ex_mat[:, indep_arr] <= ex_max[indep_arr][None, :])
                & ~np.isnan(ex_mat[:, indep_arr]),
                axis=1,
            )
            # EX lenient: NaN-as-pass
            ex_val = ex_mat[:, indep_arr]
            ex_pass_mask_nan_ok = np.all(
                np.isnan(ex_val)
                | ((ex_val >= ex_min[indep_arr][None, :]) & (ex_val <= ex_max[indep_arr][None, :])),
                axis=1,
            )
        # Under lenient policy (only one that preserves 100% ex pass): non-ex survivors
        survivors = [c for c, p in zip(ne_cl, pass_mask_lenient) if p]
        survivors.sort(key=lambda c: (c["ticker"], c["date"]))

        ex_fails = [c for c, p in zip(ex_cl, ex_pass_mask) if not p]

        lines.append("")
        lines.append("=" * 90)
        lines.append(f"{setup.upper()}  verdict: {VERDICTS[setup]}")
        lines.append(f"  deduped: ex={len(ex_cl)}  ne={len(ne_cl)}")
        lines.append(f"  top-{TOP_K} covered: {len(top)}  independent: {len(indep)}")
        ex_pass_strict = int(ex_pass_mask.sum())
        ex_pass_lenient = int(ex_pass_mask_nan_ok.sum())
        ne_pass_strict = int(pass_mask.sum())
        ne_pass_lenient = int(pass_mask_lenient.sum())
        lines.append(f"  EX pass — strict (NaN=fail):  {ex_pass_strict}/{len(ex_cl)} ({ex_pass_strict/max(len(ex_cl),1)*100:.1f}%)")
        lines.append(f"  EX pass — lenient (NaN=pass): {ex_pass_lenient}/{len(ex_cl)} ({ex_pass_lenient/max(len(ex_cl),1)*100:.1f}%)   <-- required for 100% ex coverage")
        lines.append(f"  NE pass — strict (NaN=fail):  {ne_pass_strict}/{len(ne_cl)} ({ne_pass_strict/max(len(ne_cl),1)*100:.1f}%)   (misleading — won't pass examples)")
        lines.append(f"  NE pass — lenient (NaN=pass): {ne_pass_lenient}/{len(ne_cl)} ({ne_pass_lenient/max(len(ne_cl),1)*100:.1f}%)   <-- honest, symmetric with ex")
        lines.append(f"  >>> HONEST survivors (lenient, symmetric): {len(survivors)}/{len(ne_cl)} non-ex pass")
        lines.append("=" * 90)
        if ex_fails:
            lines.append(f"  Examples failing strict AND filter (NaN-as-fail): {len(ex_fails)}")
            for e in ex_fails[:20]:
                lines.append(f"    {e['ticker']:<8}  {e['date']}")
            if len(ex_fails) > 20:
                lines.append(f"    ... and {len(ex_fails) - 20} more")
        if not survivors:
            lines.append("  (no survivors)")
            continue
        # Group by ticker for readability
        by_ticker = defaultdict(list)
        for s in survivors:
            by_ticker[s["ticker"]].append(s["date"])
        for tk in sorted(by_ticker.keys()):
            dates = sorted(by_ticker[tk])
            ex_marker = "*" if tk in ticker_has_example else " "
            lines.append(f"  {ex_marker} {tk:<8}  {', '.join(dates)}")
        n_tickers = len(by_ticker)
        n_with_ex = sum(1 for tk in by_ticker if tk in ticker_has_example)
        lines.append(f"  ── {n_tickers} unique tickers, {n_with_ex} also have labelled example(s) on same ticker (marked *)")

    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote {OUT_TXT}", flush=True)


if __name__ == "__main__":
    main()
