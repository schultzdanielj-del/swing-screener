"""Two sanity checks for the presignal grinder filters:

  1. Cross-setup: apply each setup's decorrelated top-K filter to every OTHER
     setup's non-example pool. If ~same carve rate as own pool → filters are
     generic "winner-shape", not setup-specific.
  2. Random-bar baseline: pick random (ticker, bar) samples from the universe
     (not signal-filtered), apply each setup's filter. If pass rate similar to
     filter-on-non-example-signals → pyramid grinder is adding nothing and the
     filter alone is doing the work.

Uses NaN-as-pass (lenient) policy symmetrically — the only policy that retains
100% of examples.
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
OUT_TXT = os.path.join(WORKTREE, "research", "cross_setup_and_random.txt")

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
N_RANDOM_TICKERS = 500
BARS_PER_TICKER = 20
SEED = 42


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


def lenient_pass(mat, cols, ex_min, ex_max):
    """NaN-as-pass AND filter. Returns bool mask over rows."""
    if len(cols) == 0:
        return np.zeros(mat.shape[0], dtype=bool)
    cols_arr = np.asarray(cols)
    v = mat[:, cols_arr]
    return np.all(
        np.isnan(v)
        | ((v >= ex_min[cols_arr][None, :]) & (v <= ex_max[cols_arr][None, :])),
        axis=1,
    )


def sample_random_bars(universe, n_tickers, bars_per_ticker, rng):
    """Return list of {ticker, date, bar_idx} — random sample across universe."""
    tickers = list(universe.keys())
    rng.shuffle(tickers)
    out = []
    for t in tickers:
        if len(out) >= n_tickers * bars_per_ticker:
            break
        df = universe.get(t)
        if df is None or len(df) < 200:
            continue
        ds = dates_as_str(df)
        valid_range = np.arange(120, len(df))
        if len(valid_range) < bars_per_ticker:
            continue
        picks = rng.choice(valid_range, size=bars_per_ticker, replace=False)
        for p in picks:
            out.append({"ticker": t, "date": str(ds[int(p)]), "bar_idx": int(p)})
        if len(out) >= n_tickers * bars_per_ticker:
            break
    return out[:n_tickers * bars_per_ticker]


def main():
    rng = np.random.default_rng(SEED)
    print("Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe: {len(universe)}", flush=True)
    ec = ExprSeriesCache()
    if not ec.is_valid():
        sys.exit("FAIL: expr cache invalid")

    # Phase 1: per-setup filters (bands + independent feature columns)
    print("\n--- Phase 1: build per-setup filters ---", flush=True)
    setup_data = {}
    for setup, jf in PYRAMIDS.items():
        print(f"  {setup}...", flush=True)
        raw = load_pyramid(os.path.join(CACHE_DIR, jf))
        examples = get_examples(setup)
        ebar_set = set()
        for ex in examples:
            df = universe.get(ex["ticker"])
            if df is None:
                continue
            eidx = lookup_idx(df, ex["entry_date"])
            if eidx <= 0:
                continue
            ebar_set.add((ex["ticker"], eidx - 1))
        ex_cl, ne_cl = dedupe_split(raw, universe, ebar_set)
        ex_mat = build_matrix(ex_cl, universe, ec)
        ne_mat = build_matrix(ne_cl, universe, ec)
        order, ex_min, ex_max, covered = rank(ex_mat, ne_mat)
        top = [int(c) for c in order[:TOP_K] if covered[c]]
        union_mat = np.vstack([ex_mat, ne_mat])
        indep = decorrelate(top, union_mat, CORR_THRESHOLD)
        setup_data[setup] = {
            "ex_mat": ex_mat, "ne_mat": ne_mat, "ex_cl": ex_cl, "ne_cl": ne_cl,
            "ex_min": ex_min, "ex_max": ex_max, "indep": indep,
        }
        print(f"    ex={len(ex_cl)} ne={len(ne_cl)} indep_feats={len(indep)}", flush=True)

    lines = []
    lines.append("CROSS-SETUP + RANDOM-BAR BASELINE  (NaN-as-pass lenient AND filter)")

    # Phase 2: cross-setup filter application
    print("\n--- Phase 2: cross-setup filter application ---", flush=True)
    lines.append("\n" + "=" * 90)
    lines.append("CROSS-SETUP FILTER APPLICATION")
    lines.append("=" * 90)
    lines.append("Each cell: %% of TARGET setup's non-example pool passing FILTER setup's band-set")
    header = f"{'FILTER \\\\ TARGET':<22}" + "".join(f"{t:<10}" for t in PYRAMIDS.keys())
    lines.append(header)
    for filt_setup in PYRAMIDS.keys():
        d = setup_data[filt_setup]
        row = f"{filt_setup:<22}"
        for tgt_setup in PYRAMIDS.keys():
            t = setup_data[tgt_setup]
            mask = lenient_pass(t["ne_mat"], d["indep"], d["ex_min"], d["ex_max"])
            pct = mask.sum() / max(len(t["ne_cl"]), 1) * 100
            row += f"{pct:>7.1f}%  "
        lines.append(row)
    lines.append("  (diagonal = own-pool honest carve; off-diagonal = does filter generalize to other setups' pools?)")

    # Phase 3: random-bar baseline
    print("\n--- Phase 3: random-bar sample + filter application ---", flush=True)
    print(f"  sampling {N_RANDOM_TICKERS} tickers × {BARS_PER_TICKER} bars = {N_RANDOM_TICKERS * BARS_PER_TICKER} random bars", flush=True)
    rand_clusters = sample_random_bars(universe, N_RANDOM_TICKERS, BARS_PER_TICKER, rng)
    print(f"  sampled {len(rand_clusters)} bars, building feature matrix...", flush=True)
    rand_mat = build_matrix(rand_clusters, universe, ec)
    # Drop rows with all-nan (failed loads)
    any_valid = np.any(~np.isnan(rand_mat), axis=1)
    rand_mat = rand_mat[any_valid]
    print(f"  {rand_mat.shape[0]} rows with data", flush=True)

    lines.append("\n" + "=" * 90)
    lines.append(f"RANDOM-BAR BASELINE  ({rand_mat.shape[0]} random bars from universe, NOT signal-filtered)")
    lines.append("=" * 90)
    lines.append(f"{'FILTER':<22} {'own-ne pass%':<15} {'random-bar pass%':<20} {'ratio (own/rand)':<15}")
    for setup in PYRAMIDS.keys():
        d = setup_data[setup]
        own_mask = lenient_pass(d["ne_mat"], d["indep"], d["ex_min"], d["ex_max"])
        own_pct = own_mask.sum() / max(len(d["ne_cl"]), 1) * 100
        rand_mask = lenient_pass(rand_mat, d["indep"], d["ex_min"], d["ex_max"])
        rand_pct = rand_mask.sum() / max(rand_mat.shape[0], 1) * 100
        ratio = own_pct / max(rand_pct, 0.001)
        lines.append(f"{setup:<22} {own_pct:>10.1f}%     {rand_pct:>15.1f}%     {ratio:>8.2f}x")
    lines.append("  (ratio > 1 means pyramid signal pool is enriched for filter-passing vs random)")
    lines.append("  (ratio ~= 1 means filter catches same rate on random bars — pyramid adds nothing)")

    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote {OUT_TXT}", flush=True)


if __name__ == "__main__":
    main()
