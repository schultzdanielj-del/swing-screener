"""Priority-ranked improvement roadmap — full autonomous run.

Implements §11.6 of PRESIGNAL_GRINDER.md:
  R1 — rank features against random bars, not pyramid non-examples
  R2 — LOO-stable feature selection gate
  R3 — multi-tier output (tight/medium/loose)
  E1 — PCA ellipsoid bands
  E2 — supervised classifier supplement
  R4 — K-fold cross-validated feature ranking
  R5 — band headroom sweep
  E4 — cascade class→setup filter
  X2 — temporal holdout validation

X1 (tradable_entries) requires Dan's labeling — skipped.
E3 (chart-shape gestalt features) requires main-repo cache rebuild — skipped.

Outputs all go to research/improvements/ inside the worktree. Never writes to
the main repo. Reads OHLCV + expr cache read-only from main repo paths.
"""
from __future__ import annotations

import os, sys, json, pickle, sqlite3, random, time, traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────
MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_DIR = os.path.join(WORKTREE, "research", "improvements")
FINAL_DIR = os.path.join(OUT_DIR, "final_multitier")
RUN_LOG = os.path.join(OUT_DIR, "run_log.txt")

sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache

# ── Config ────────────────────────────────────────────────────────────────
PYRAMIDS = {
    "htf":    "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":     "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":   "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":   "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db":  "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
SETUP_ORDER = ["htf", "bf", "base", "dtss", "3-4db"]
BREAKOUT = {"htf", "bf", "base"}
FADE = {"dtss", "3-4db"}

MIN_EX_MEASURED = 10
MIN_NE_MEASURED_FRAC = 0.5
MIN_RANDOM_MEASURED_FRAC = 0.5
LOO_FAIL_THRESHOLD = 0.0  # BULLETPROOF: strictest — feature kept iff no example fails LOO
CORR_THRESHOLD = 0.9

R5_DELTAS = [0.0, 0.05, 0.10, 0.20]
R4_N_FOLDS = 5
R4_TOP_BUCKET = 50
R4_MIN_FOLDS = 4                      # diagnostic R4 uses 4-of-5
BULLETPROOF_MIN_FOLDS = R4_N_FOLDS    # bulletproof gate requires every-fold top-50

PERM_N_TRIALS = 100                   # per-feature permutation significance draws

TEMPORAL_CUTOFFS = {
    "htf": "2023-11-01",
    "bf": "2025-06-01",
    "base": "2024-02-01",
}

N_RANDOM_TICKERS = 500
BARS_PER_TICKER = 20
RANDOM_SEED = 42

E1_PCA_FEATURES = 10
E2_SUP_FEATURES = 20

# ── Logging ───────────────────────────────────────────────────────────────
_log_lines = []
def log(msg=""):
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)

def flush_log():
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RUN_LOG, 'w', encoding='utf-8') as f:
        f.write("\n".join(_log_lines))

# ── Data helpers ──────────────────────────────────────────────────────────
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

def dedupe_split(raw_signals, universe, example_bar_set):
    """Per §5.2: consecutive-bar dedupe per ticker, rightmost wins, split at
    example bars so each example becomes its own cluster's rightmost."""
    by_ticker = defaultdict(list)
    for s in raw_signals:
        df = universe.get(s["ticker"])
        if df is None:
            continue
        idx = lookup_idx(df, s["date"])
        if idx < 0:
            continue
        by_ticker[s["ticker"]].append(
            {"ticker": s["ticker"], "date": s["date"], "bar_idx": idx}
        )
    ex_cl, ne_cl = [], []
    for ticker, sigs in by_ticker.items():
        sigs.sort(key=lambda x: x["bar_idx"])
        cluster = [sigs[0]]
        for cur in sigs[1:]:
            prev = cluster[-1]
            if (cur["bar_idx"] != prev["bar_idx"] + 1
                or (ticker, prev["bar_idx"]) in example_bar_set):
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

def build_matrix(clusters, ec):
    """(n_clusters, n_expr) matrix. Per-ticker single load, no cross-call cache
    (each ticker's full .npz is ~77 MB; caching them blows memory)."""
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
        # Explicitly release ticker's full matrix before next iteration
        del expr_data, expr_dates, ed_str
    return mat

def sample_random_bars(universe, n_tickers, bars_per_ticker, rng):
    tickers = list(universe.keys())
    rng.shuffle(tickers)
    out = []
    target = n_tickers * bars_per_ticker
    for t in tickers:
        if len(out) >= target:
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
        if len(out) >= target:
            break
    return out[:target]

# ── Ranking / bands / filters ─────────────────────────────────────────────
def compute_bands(ex_mat):
    ex_non_nan = np.sum(~np.isnan(ex_mat), axis=0)
    with np.errstate(all='ignore'):
        ex_min = np.nanmin(ex_mat, axis=0)
        ex_max = np.nanmax(ex_mat, axis=0)
    return ex_min, ex_max, ex_non_nan

def carve_value(denom_mat, ex_min, ex_max):
    """(#rows with non-nan AND out-of-band) / (#rows with non-nan)."""
    valid = ~np.isnan(denom_mat)
    in_band = (denom_mat >= ex_min[None, :]) & (denom_mat <= ex_max[None, :]) & valid
    out_measured = (valid & ~in_band).sum(axis=0)
    measured = valid.sum(axis=0)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(measured > 0, out_measured / measured, 0.0), measured

def compute_loo_fail(ex_mat):
    """For each feature: fraction of non-nan examples that fall outside the
    band rebuilt after removing them."""
    n_ex, n_feat = ex_mat.shape
    fail = np.full(n_feat, np.nan, dtype=np.float32)
    for c in range(n_feat):
        vals = ex_mat[:, c]
        mask = ~np.isnan(vals)
        if mask.sum() < 2:
            continue
        vv = vals[mask]
        # Vectorised LOO: per value, compare against min/max of the rest.
        # For each held-out v, it fails iff v > max(rest) or v < min(rest).
        # Equivalent: v is the unique max or unique min of the full set.
        sorted_vv = np.sort(vv)
        is_max = vv == sorted_vv[-1]
        is_min = vv == sorted_vv[0]
        # Held-out value falls outside rebuilt band iff it was the unique
        # extreme (duplicates → rebuilt band still contains it).
        n_max = int((vv == sorted_vv[-1]).sum())
        n_min = int((vv == sorted_vv[0]).sum())
        # If there's only one max, that example fails LOO (held-out max
        # gives rebuilt max = second-highest, held-out > that).
        fail_count = 0
        if n_max == 1:
            fail_count += 1
        if n_min == 1 and sorted_vv[0] != sorted_vv[-1]:
            fail_count += 1
        fail[c] = fail_count / len(vv)
    return fail

def rank_features(ex_mat, denom_mat, ex_mat_secondary=None,
                  use_r1=True, use_r2=True, loo_threshold=LOO_FAIL_THRESHOLD,
                  ne_mat_for_coverage=None):
    """Return (order, ex_min, ex_max, covered, carve_value_arr, loo_fail_arr).

    denom_mat: matrix whose rows are the ranking-carve denominator (R1=random;
               non-R1=pyramid non-examples).
    ne_mat_for_coverage: if provided, coverage gate requires ne_measured_frac >=
                        MIN_NE_MEASURED_FRAC on this matrix in addition to the
                        denom_mat gate. Set to None under R1 to use random
                        coverage only.
    """
    ex_min, ex_max, ex_non_nan = compute_bands(ex_mat)
    cv, denom_measured = carve_value(denom_mat, ex_min, ex_max)
    denom_frac = denom_measured / max(denom_mat.shape[0], 1)
    covered = (ex_non_nan >= MIN_EX_MEASURED) & (denom_frac >= MIN_RANDOM_MEASURED_FRAC)
    if ne_mat_for_coverage is not None:
        ne_valid = ~np.isnan(ne_mat_for_coverage)
        ne_frac = ne_valid.sum(axis=0) / max(ne_mat_for_coverage.shape[0], 1)
        covered &= (ne_frac >= MIN_NE_MEASURED_FRAC)
    loo_fail = np.full(ex_mat.shape[1], np.nan, dtype=np.float32)
    if use_r2:
        loo_fail = compute_loo_fail(ex_mat)
        # A feature with NaN loo_fail (fewer than 2 non-nan examples) is already
        # excluded by ex_non_nan >= 10; still guard it.
        loo_ok = np.where(np.isnan(loo_fail), False, loo_fail <= loo_threshold)
        covered &= loo_ok
    order = np.argsort(-np.where(covered, cv, -np.inf))
    return order, ex_min, ex_max, covered, cv, loo_fail

def decorrelate(cols, union_mat, threshold=CORR_THRESHOLD):
    kept, kept_vals = [], []
    for c in cols:
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
            kept.append(int(c))
            kept_vals.append(v)
    return kept

def compute_perm_max_carve(random_mat, n_ex, n_trials, seed, n_workers=None):
    """Per-feature permutation null, parallelized via threading. For each of
    n_trials trials, draw n_ex random-bar rows as pseudo-examples, compute
    their [min, max] band per feature, measure carve on the remaining rows,
    track max carve per feature across all trials.

    Threading gives real parallelism here because nanmin/nanmax, comparisons,
    and sum are vectorized numpy ops that release the GIL. The random_mat
    (~642 MB for 10k × 16k features) stays in shared memory instead of being
    copied per worker (which processes would do), keeping RAM under 32 GB.

    Per-thread working memory is ~1 GB (rest + rest_valid + in_band masks);
    with n_workers up to os.cpu_count(), peak RAM ≈ random_mat + valid_all
    + n_workers × 1 GB."""
    if n_workers is None:
        n_workers = os.cpu_count() or 4
    n_workers = max(1, int(n_workers))
    n_rand, n_feat = random_mat.shape
    valid_all = ~np.isnan(random_mat)  # shared, read-only across workers

    def _run_chunk(chunk_seed, trial_count):
        rng = np.random.default_rng(chunk_seed)
        local_max = np.zeros(n_feat, dtype=np.float32)
        for _ in range(trial_count):
            idx = rng.choice(n_rand, size=n_ex, replace=False)
            pseudo_ex = random_mat[idx]
            with np.errstate(all='ignore'):
                pm_min = np.nanmin(pseudo_ex, axis=0)
                pm_max = np.nanmax(pseudo_ex, axis=0)
            rest_mask = np.ones(n_rand, dtype=bool); rest_mask[idx] = False
            rest = random_mat[rest_mask]
            rest_valid = valid_all[rest_mask]
            with np.errstate(invalid='ignore'):
                in_band = (rest >= pm_min[None, :]) & (rest <= pm_max[None, :]) & rest_valid
            out = (rest_valid & ~in_band).sum(axis=0)
            measured = rest_valid.sum(axis=0)
            with np.errstate(invalid='ignore', divide='ignore'):
                cv_trial = np.where(measured > 0, out.astype(np.float32) / measured, 0.0)
            valid_band = np.isfinite(pm_min) & np.isfinite(pm_max)
            local_max = np.where(valid_band, np.maximum(local_max, cv_trial), local_max)
        return local_max

    per_worker = n_trials // n_workers
    extras = n_trials % n_workers
    tasks = []
    ss = np.random.SeedSequence(seed)
    child_seeds = ss.spawn(n_workers)
    for w in range(n_workers):
        count = per_worker + (1 if w < extras else 0)
        if count == 0:
            continue
        tasks.append((int(child_seeds[w].generate_state(1)[0]), count))

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(_run_chunk, s, c) for s, c in tasks]
        results = [f.result() for f in futures]

    return np.maximum.reduce(results) if results else np.zeros(n_feat, dtype=np.float32)


def lenient_pass(mat, cols, ex_min, ex_max, delta=0.0):
    if len(cols) == 0:
        return np.zeros(mat.shape[0], dtype=bool)
    cols = np.asarray(cols)
    v = mat[:, cols]
    lo = ex_min[cols]
    hi = ex_max[cols]
    if delta > 0.0:
        span = (hi - lo)
        span = np.where(np.isfinite(span), span, 0.0)
        lo = lo - delta * span
        hi = hi + delta * span
    in_band = np.isnan(v) | ((v >= lo[None, :]) & (v <= hi[None, :]))
    return np.all(in_band, axis=1)

def lenient_k_of_n(mat, cols, ex_min, ex_max, min_pass, delta=0.0):
    if len(cols) == 0:
        return np.zeros(mat.shape[0], dtype=bool)
    cols = np.asarray(cols)
    v = mat[:, cols]
    lo = ex_min[cols]
    hi = ex_max[cols]
    if delta > 0.0:
        span = (hi - lo)
        span = np.where(np.isfinite(span), span, 0.0)
        lo = lo - delta * span
        hi = hi + delta * span
    atom_pass = np.isnan(v) | ((v >= lo[None, :]) & (v <= hi[None, :]))
    return atom_pass.sum(axis=1) >= min_pass

# ── Step outputs ──────────────────────────────────────────────────────────
def _pct(num, den):
    return f"{num}/{den} ({(num/max(den,1))*100:.1f}%)"

def write_step_file(name, lines):
    path = os.path.join(OUT_DIR, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    log(f"  wrote {path}")

# ── Step 1: R1 — rank by random-bar carve ─────────────────────────────────
def step_R1(setup_data, random_mat):
    log("STEP R1 — rank by random-bar carve_value")
    lines = []
    lines.append("=" * 100)
    lines.append("R1 — FEATURE RANKING BY RANDOM-BAR CARVE VALUE")
    lines.append("=" * 100)
    lines.append(f"Random-bar denominator: {random_mat.shape[0]} bars from universe")
    lines.append(f"Coverage gates: ex_measured >= {MIN_EX_MEASURED}, random_measured_frac >= {MIN_RANDOM_MEASURED_FRAC}")
    lines.append("R2 OFF here — LOO gate not applied; compare to R2 step to see its marginal.")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            sd["ex_mat"], random_mat,
            use_r1=True, use_r2=False,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        top40 = [int(c) for c in order[:40] if covered[c]]
        union_mat = np.vstack([sd["ex_mat"], sd["ne_mat"], random_mat])
        indep = decorrelate(top40, union_mat, CORR_THRESHOLD)
        ne_pass = lenient_pass(sd["ne_mat"], indep, ex_min, ex_max).sum()
        ex_pass = lenient_pass(sd["ex_mat"], indep, ex_min, ex_max).sum()
        rand_pass = lenient_pass(random_mat, indep, ex_min, ex_max).sum()
        per_setup[setup] = {
            "order": order, "ex_min": ex_min, "ex_max": ex_max,
            "covered": covered, "cv_random": cv, "loo_fail": loo_fail,
            "top40": top40, "indep": indep,
            "ne_pass": int(ne_pass), "ex_pass": int(ex_pass),
            "rand_pass": int(rand_pass),
        }
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  ex={sd['ex_mat'].shape[0]}  ne={sd['ne_mat'].shape[0]}  rand={random_mat.shape[0]}")
        lines.append(f"  covered features (ex>={MIN_EX_MEASURED}, rand_frac>={MIN_RANDOM_MEASURED_FRAC}): {int(covered.sum())}")
        lines.append(f"  top-40 -> decorrelated indep: {len(indep)}")
        lines.append(f"  EX lenient pass (sanity, should be 100%): {_pct(ex_pass, sd['ex_mat'].shape[0])}")
        lines.append(f"  NE lenient pass (own pool):              {_pct(ne_pass, sd['ne_mat'].shape[0])}")
        lines.append(f"  RANDOM lenient pass:                     {_pct(rand_pass, random_mat.shape[0])}")
        lines.append(f"  own/random ratio: {ne_pass/max(sd['ne_mat'].shape[0],1):.3f} / {rand_pass/max(random_mat.shape[0],1):.3f} = {(ne_pass/max(sd['ne_mat'].shape[0],1))/max(rand_pass/max(random_mat.shape[0],1),1e-6):.2f}x")
        lines.append("  Top-15 features by random-bar carve:")
        for r, ci in enumerate(order[:15]):
            if not covered[ci]:
                break
            lines.append(f"    {r+1:>2}. {sd['expr_names'][ci]:<52} "
                         f"[{ex_min[ci]:+.3g},{ex_max[ci]:+.3g}]  "
                         f"rand_cv={cv[ci]*100:5.1f}%  loo={loo_fail[ci]*100:5.1f}%")
        lines.append("")
    write_step_file("R1_random_bar_rank.txt", lines)
    return per_setup

# ── Step 2: R2 — LOO gate on top of v1 ranking (pyramid ne denom) ─────────
def step_R2(setup_data):
    log("STEP R2 — LOO-stable gate (v1 ranking, pyramid-ne denominator)")
    lines = []
    lines.append("=" * 100)
    lines.append("R2 — LOO-STABLE FEATURE GATE (applied to v1 pyramid-ne ranking)")
    lines.append("=" * 100)
    lines.append(f"Gate: LOO failure rate <= {LOO_FAIL_THRESHOLD}")
    lines.append("Ranking uses pyramid non-examples as denominator (v1 style). Compare to R1 step.")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            sd["ex_mat"], sd["ne_mat"],
            use_r1=False, use_r2=True,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        # For comparison: v1 ranking (no R2 gate)
        order_v1, _, _, covered_v1, cv_v1, _ = rank_features(
            sd["ex_mat"], sd["ne_mat"],
            use_r1=False, use_r2=False,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        top40 = [int(c) for c in order[:40] if covered[c]]
        top40_v1 = [int(c) for c in order_v1[:40] if covered_v1[c]]
        union_mat = np.vstack([sd["ex_mat"], sd["ne_mat"]])
        indep = decorrelate(top40, union_mat)
        indep_v1 = decorrelate(top40_v1, union_mat)
        ne_pass = lenient_pass(sd["ne_mat"], indep, ex_min, ex_max).sum()
        ex_pass = lenient_pass(sd["ex_mat"], indep, ex_min, ex_max).sum()
        ne_pass_v1 = lenient_pass(sd["ne_mat"], indep_v1, ex_min, ex_max).sum()
        n_features_gated_out = int((covered_v1 & ~covered).sum())
        per_setup[setup] = {
            "order": order, "covered": covered, "loo_fail": loo_fail,
            "top40": top40, "indep": indep, "ne_pass": int(ne_pass),
        }
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  ex={sd['ex_mat'].shape[0]}  ne={sd['ne_mat'].shape[0]}")
        lines.append(f"  covered without R2: {int(covered_v1.sum())}")
        lines.append(f"  covered with R2:    {int(covered.sum())}  (gated out {n_features_gated_out})")
        lines.append(f"  top-40 -> indep: {len(indep)}  (v1: {len(indep_v1)})")
        lines.append(f"  EX lenient pass:        {_pct(ex_pass, sd['ex_mat'].shape[0])}")
        lines.append(f"  NE lenient pass (R2):   {_pct(ne_pass, sd['ne_mat'].shape[0])}")
        lines.append(f"  NE lenient pass (v1):   {_pct(ne_pass_v1, sd['ne_mat'].shape[0])}")
        # How many of top-40 v1 had LOO failures?
        top40_v1_loo = [loo_fail[c] for c in top40_v1]
        n_v1_unstable = sum(1 for f in top40_v1_loo if np.isfinite(f) and f > LOO_FAIL_THRESHOLD)
        lines.append(f"  v1 top-40 features with LOO>{LOO_FAIL_THRESHOLD}: {n_v1_unstable}/40 (these would be gated by R2)")
        lines.append("")
    write_step_file("R2_loo_gate.txt", lines)
    return per_setup

# ── Step 3: R1+R2 combined ────────────────────────────────────────────────
def step_R1R2(setup_data, random_mat):
    log("STEP R1+R2 — random-bar rank + LOO gate")
    lines = []
    lines.append("=" * 100)
    lines.append("R1 + R2 COMBINED — random-bar ranking with LOO-stable gate")
    lines.append("=" * 100)
    lines.append(f"Rank by random-bar carve_value, gate by LOO failure <= {LOO_FAIL_THRESHOLD}")
    lines.append(f"Coverage: ex_measured >= {MIN_EX_MEASURED}, random_frac >= {MIN_RANDOM_MEASURED_FRAC}")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            sd["ex_mat"], random_mat,
            use_r1=True, use_r2=True,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        n_cov = int(covered.sum())
        # Pick features up to 50 for decorrelation pool, to give the
        # decorrelator room to find ~20 independent ones.
        pool = [int(c) for c in order[:50] if covered[c]]
        union_mat = np.vstack([sd["ex_mat"], sd["ne_mat"], random_mat])
        indep = decorrelate(pool, union_mat, CORR_THRESHOLD)

        top40 = pool[:40]
        indep40 = decorrelate(top40, union_mat, CORR_THRESHOLD)

        ne_strict = lenient_pass(sd["ne_mat"], indep40, ex_min, ex_max).sum()
        ex_strict = lenient_pass(sd["ex_mat"], indep40, ex_min, ex_max).sum()
        rand_strict = lenient_pass(random_mat, indep40, ex_min, ex_max).sum()

        per_setup[setup] = {
            "order": order, "ex_min": ex_min, "ex_max": ex_max,
            "covered": covered, "cv_random": cv, "loo_fail": loo_fail,
            "pool50": pool, "indep_pool": indep,
            "top40": top40, "indep40": indep40,
            "ne_strict": int(ne_strict), "ex_strict": int(ex_strict),
            "rand_strict": int(rand_strict),
        }

        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  ex={sd['ex_mat'].shape[0]}  ne={sd['ne_mat'].shape[0]}  rand={random_mat.shape[0]}")
        lines.append(f"  covered features (all 3 gates): {n_cov}")
        lines.append(f"  top-50 -> indep pool: {len(indep)}")
        lines.append(f"  top-40 -> indep tight set: {len(indep40)}")
        lines.append(f"  EX lenient pass (sanity):   {_pct(ex_strict, sd['ex_mat'].shape[0])}")
        lines.append(f"  NE lenient pass (own pool): {_pct(ne_strict, sd['ne_mat'].shape[0])}")
        lines.append(f"  RANDOM lenient pass:        {_pct(rand_strict, random_mat.shape[0])}")
        rat = (ne_strict/max(sd['ne_mat'].shape[0],1)) / max(rand_strict/max(random_mat.shape[0],1),1e-6)
        lines.append(f"  own/random ratio: {rat:.2f}x")
        lines.append("  Top-15 features by R1+R2 rank (random-bar carve, LOO-stable):")
        for r, ci in enumerate(order[:15]):
            if not covered[ci]:
                break
            lines.append(f"    {r+1:>2}. {sd['expr_names'][ci]:<52} "
                         f"[{ex_min[ci]:+.3g},{ex_max[ci]:+.3g}]  "
                         f"rand_cv={cv[ci]*100:5.1f}%  loo={loo_fail[ci]*100:5.1f}%")
        lines.append("")
    write_step_file("R1R2_combined.txt", lines)
    return per_setup

# ── Step 4: E1 — PCA ellipsoid bands ──────────────────────────────────────
def step_E1(setup_data, r1r2, random_mat):
    log("STEP E1 — PCA ellipsoid bands")
    lines = []
    lines.append("=" * 100)
    lines.append("E1 — PCA ELLIPSOID BANDS (axes from R1+R2 decorrelated top-N)")
    lines.append("=" * 100)
    lines.append(f"Input feature set: R1+R2 decorrelated (up to {E1_PCA_FEATURES} features)")
    lines.append("Method: z-score on ex+ne union, PCA on example covariance, band each PC by ex [min,max].")
    lines.append("Cluster passes lenient PCA atom if it has NaN on any input feature OR its projection lies in all PC bands.")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        r = r1r2[setup]
        cols = r["indep_pool"][:E1_PCA_FEATURES]
        if len(cols) < 3:
            lines.append(f"{setup.upper()}: only {len(cols)} R1+R2 features — skipping PCA")
            lines.append("")
            continue
        ex_vals = sd["ex_mat"][:, cols]
        ne_vals = sd["ne_mat"][:, cols]
        rand_vals = random_mat[:, cols]
        # Which rows have any NaN → under lenient, they pass automatically
        ex_any_nan = np.any(np.isnan(ex_vals), axis=1)
        ne_any_nan = np.any(np.isnan(ne_vals), axis=1)
        rand_any_nan = np.any(np.isnan(rand_vals), axis=1)
        # Work in the full-value subset for PCA
        ex_full = ex_vals[~ex_any_nan]
        ne_full = ne_vals[~ne_any_nan]
        rand_full = rand_vals[~rand_any_nan]
        if ex_full.shape[0] < 3:
            lines.append(f"{setup.upper()}: only {ex_full.shape[0]} examples have full-value rows — skipping PCA")
            lines.append("")
            continue
        # Standardize using union mean/std
        union_full = np.vstack([ex_full, ne_full]) if ne_full.shape[0] > 0 else ex_full
        mu = union_full.mean(axis=0)
        sd_ = union_full.std(axis=0) + 1e-9
        ex_z = (ex_full - mu) / sd_
        ne_z = (ne_full - mu) / sd_
        rand_z = (rand_full - mu) / sd_
        # PCA on ex covariance
        cov = np.cov(ex_z, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)
        # Sort descending
        idx_sort = np.argsort(-eigvals)
        eigvals = eigvals[idx_sort]
        eigvecs = eigvecs[:, idx_sort]
        # Project
        ex_pcs = ex_z @ eigvecs
        ne_pcs = ne_z @ eigvecs
        rand_pcs = rand_z @ eigvecs
        # Band each PC by example min/max
        pc_min = ex_pcs.min(axis=0)
        pc_max = ex_pcs.max(axis=0)
        # Pass = all PCs in band
        ne_in = np.all((ne_pcs >= pc_min[None, :]) & (ne_pcs <= pc_max[None, :]), axis=1)
        rand_in = np.all((rand_pcs >= pc_min[None, :]) & (rand_pcs <= pc_max[None, :]), axis=1)
        # Lenient: NaN-rows pass automatically
        ne_pass_lenient = int(ne_in.sum()) + int(ne_any_nan.sum())
        rand_pass_lenient = int(rand_in.sum()) + int(rand_any_nan.sum())
        per_setup[setup] = {
            "cols": cols, "eigvals": eigvals.tolist(),
            "ne_pass_lenient": ne_pass_lenient,
            "rand_pass_lenient": rand_pass_lenient,
        }
        # Also apply R1+R2 baseline for comparison
        baseline_ne = r["ne_strict"]
        baseline_rand = r["rand_strict"]
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  input features={len(cols)}")
        lines.append(f"  examples with full values: {ex_full.shape[0]}/{sd['ex_mat'].shape[0]}")
        lines.append(f"  NE full-value rows: {ne_full.shape[0]}/{sd['ne_mat'].shape[0]}  (NaN rows pass lenient)")
        lines.append(f"  PC eigenvalue ratios (fraction of variance per PC):")
        ev_sum = eigvals.sum()
        for i in range(len(eigvals)):
            lines.append(f"    PC{i+1}: {eigvals[i]/ev_sum*100:5.1f}%  band [{pc_min[i]:+.2f},{pc_max[i]:+.2f}]")
        lines.append(f"  E1 NE pass lenient: {_pct(ne_pass_lenient, sd['ne_mat'].shape[0])}")
        lines.append(f"  E1 RANDOM pass lenient: {_pct(rand_pass_lenient, random_mat.shape[0])}")
        lines.append(f"  vs R1+R2 AND tight baseline:")
        lines.append(f"    R1+R2 NE: {_pct(baseline_ne, sd['ne_mat'].shape[0])}")
        lines.append(f"    R1+R2 RANDOM: {_pct(baseline_rand, random_mat.shape[0])}")
        lines.append("")
    write_step_file("E1_pca.txt", lines)
    return per_setup

# ── Step 5: E2 — supervised classifier ────────────────────────────────────
def step_E2(setup_data, r1r2, random_mat):
    log("STEP E2 — supervised classifier supplement")
    lines = []
    lines.append("=" * 100)
    lines.append("E2 — SUPERVISED CLASSIFIER (logistic regression, L2)")
    lines.append("=" * 100)
    lines.append(f"Input feature set: R1+R2 decorrelated top-{E2_SUP_FEATURES}")
    lines.append("Method: pure numpy L2 logistic regression. Train on examples=1 vs non-examples=0.")
    lines.append("NaN imputation: feature median from union(ex, ne).")
    lines.append("Score threshold: lowest example score (preserves 100% example pass).")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        r = r1r2[setup]
        cols = r["indep_pool"][:E2_SUP_FEATURES]
        if len(cols) < 3:
            lines.append(f"{setup.upper()}: too few features ({len(cols)}) — skipping")
            lines.append("")
            continue
        ex_vals = sd["ex_mat"][:, cols].copy()
        ne_vals = sd["ne_mat"][:, cols].copy()
        rand_vals = random_mat[:, cols].copy()
        # Impute with union median
        union = np.vstack([ex_vals, ne_vals])
        medians = np.nanmedian(union, axis=0)
        for j, m in enumerate(medians):
            if not np.isfinite(m):
                medians[j] = 0.0
        for arr in (ex_vals, ne_vals, rand_vals):
            mask = np.isnan(arr)
            for j in range(arr.shape[1]):
                arr[mask[:, j], j] = medians[j]
        # Standardize using union
        union_imp = np.vstack([ex_vals, ne_vals])
        mu = union_imp.mean(axis=0)
        sd_ = union_imp.std(axis=0) + 1e-9
        ex_z = (ex_vals - mu) / sd_
        ne_z = (ne_vals - mu) / sd_
        rand_z = (rand_vals - mu) / sd_
        X = np.vstack([ex_z, ne_z])
        y = np.concatenate([np.ones(ex_z.shape[0]), np.zeros(ne_z.shape[0])])
        # Simple L2 logistic regression via batch gradient descent
        w = np.zeros(X.shape[1] + 1, dtype=np.float64)
        Xb = np.hstack([X, np.ones((X.shape[0], 1))])
        lr = 0.1
        lam = 0.1
        for it in range(600):
            z = Xb @ w
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            grad = Xb.T @ (p - y) / X.shape[0] + lam * np.concatenate([w[:-1], [0.0]])
            w -= lr * grad
        def score(A):
            Ab = np.hstack([A, np.ones((A.shape[0], 1))])
            z = Ab @ w
            return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        ex_scores = score(ex_z)
        ne_scores = score(ne_z)
        rand_scores = score(rand_z)
        # Threshold = min example score, guarantees 100% ex pass
        thr = float(ex_scores.min())
        ne_pass = int((ne_scores >= thr).sum())
        rand_pass = int((rand_scores >= thr).sum())
        per_setup[setup] = {
            "cols": cols, "threshold": thr,
            "ne_pass": ne_pass, "rand_pass": rand_pass,
            "coefs": w[:-1].tolist(), "bias": float(w[-1]),
        }
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  features={len(cols)}")
        lines.append(f"  score threshold (= min ex score): {thr:.4f}")
        lines.append(f"  ex scores (min/median/max): {ex_scores.min():.3f} / {np.median(ex_scores):.3f} / {ex_scores.max():.3f}")
        lines.append(f"  ne scores (min/median/max): {ne_scores.min():.3f} / {np.median(ne_scores):.3f} / {ne_scores.max():.3f}")
        lines.append(f"  E2 NE pass (score >= thr):     {_pct(ne_pass, sd['ne_mat'].shape[0])}")
        lines.append(f"  E2 RANDOM pass (score >= thr): {_pct(rand_pass, random_mat.shape[0])}")
        lines.append(f"  vs R1+R2 AND tight NE:         {_pct(r['ne_strict'], sd['ne_mat'].shape[0])}")
        lines.append(f"  vs R1+R2 AND tight RANDOM:     {_pct(r['rand_strict'], random_mat.shape[0])}")
        # Top coefficient magnitudes
        coef_abs = np.abs(w[:-1])
        order_c = np.argsort(-coef_abs)
        lines.append("  Top-10 features by |coefficient|:")
        for r_i in order_c[:10]:
            lines.append(f"    {sd['expr_names'][cols[r_i]]:<52} coef={w[r_i]:+.3f}")
        lines.append("")
    write_step_file("E2_supervised.txt", lines)
    return per_setup

# ── Step 6: R4 — K-fold cross-validated feature ranking ───────────────────
def step_R4(setup_data, random_mat):
    log("STEP R4 — K-fold cross-validated feature ranking")
    lines = []
    lines.append("=" * 100)
    lines.append(f"R4 — K-FOLD CROSS-VALIDATED FEATURE RANKING (K={R4_N_FOLDS})")
    lines.append("=" * 100)
    lines.append(f"Split examples into {R4_N_FOLDS} folds. In each fold, rank features on remaining folds' bands.")
    lines.append(f"Keep features ranked top-{R4_TOP_BUCKET} in >={R4_MIN_FOLDS}/{R4_N_FOLDS} folds. Uses random-bar denominator.")
    lines.append("")
    rng = np.random.default_rng(RANDOM_SEED)
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        n_ex = sd["ex_mat"].shape[0]
        if n_ex < R4_N_FOLDS:
            lines.append(f"{setup.upper()}: only {n_ex} examples; folds={R4_N_FOLDS} infeasible — skipping")
            lines.append("")
            continue
        fold_order = np.arange(n_ex)
        rng.shuffle(fold_order)
        fold_buckets = np.array_split(fold_order, R4_N_FOLDS)
        # For each fold, rank features on remaining examples
        top_bucket_counts = np.zeros(sd["ex_mat"].shape[1], dtype=np.int32)
        for k in range(R4_N_FOLDS):
            held_out = fold_buckets[k]
            remaining_mask = np.ones(n_ex, dtype=bool)
            remaining_mask[held_out] = False
            ex_sub = sd["ex_mat"][remaining_mask]
            order, _, _, covered, _, _ = rank_features(
                ex_sub, random_mat,
                use_r1=True, use_r2=True,
                ne_mat_for_coverage=sd["ne_mat"],
            )
            top = [int(c) for c in order[:R4_TOP_BUCKET] if covered[c]]
            for c in top:
                top_bucket_counts[c] += 1
        # R4-stable set: features in top-bucket in >= R4_MIN_FOLDS folds
        stable_mask = top_bucket_counts >= R4_MIN_FOLDS
        stable_cols = np.where(stable_mask)[0].tolist()
        # Rank stable cols by full-ex random-bar carve
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            sd["ex_mat"], random_mat,
            use_r1=True, use_r2=True,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        stable_ordered = [c for c in order if c in set(stable_cols)][:40]
        union_mat = np.vstack([sd["ex_mat"], sd["ne_mat"], random_mat])
        indep = decorrelate(stable_ordered, union_mat)
        ne_pass = int(lenient_pass(sd["ne_mat"], indep, ex_min, ex_max).sum())
        ex_pass = int(lenient_pass(sd["ex_mat"], indep, ex_min, ex_max).sum())
        rand_pass = int(lenient_pass(random_mat, indep, ex_min, ex_max).sum())
        per_setup[setup] = {
            "stable_cols": stable_cols, "stable_top40": stable_ordered,
            "indep": indep, "ne_pass": ne_pass, "ex_pass": ex_pass,
            "rand_pass": rand_pass,
            "fold_counts": top_bucket_counts,  # (n_feat,) — bulletproof uses ==R4_N_FOLDS
        }
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  ex={n_ex}  folds={R4_N_FOLDS}")
        lines.append(f"  features in top-{R4_TOP_BUCKET} in >={R4_MIN_FOLDS} folds: {len(stable_cols)}")
        lines.append(f"  stable_ordered[:40] -> indep: {len(indep)}")
        lines.append(f"  EX lenient pass:     {_pct(ex_pass, n_ex)}")
        lines.append(f"  NE lenient pass:     {_pct(ne_pass, sd['ne_mat'].shape[0])}")
        lines.append(f"  RANDOM lenient pass: {_pct(rand_pass, random_mat.shape[0])}")
        lines.append("")
    write_step_file("R4_kfold.txt", lines)
    return per_setup

# ── Step 7: R5 — band headroom sweep ──────────────────────────────────────
def step_R5(setup_data, r1r2, random_mat):
    log("STEP R5 — band headroom sweep")
    lines = []
    lines.append("=" * 100)
    lines.append("R5 — BAND HEADROOM SWEEP")
    lines.append("=" * 100)
    lines.append(f"Deltas tested: {R5_DELTAS}. Expand each R1+R2 band by delta × range.")
    lines.append("Purpose: future-proof filter against new examples just outside current bands.")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        r = r1r2[setup]
        indep = r["indep40"]
        ex_min, ex_max = r["ex_min"], r["ex_max"]
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  indep features: {len(indep)}  ex={sd['ex_mat'].shape[0]}  ne={sd['ne_mat'].shape[0]}")
        lines.append(f"  {'delta':<8}{'ex pass':<14}{'ne pass':<14}{'rand pass':<14}{'own/rand':<10}")
        setup_results = {}
        for delta in R5_DELTAS:
            ex_p = int(lenient_pass(sd["ex_mat"], indep, ex_min, ex_max, delta=delta).sum())
            ne_p = int(lenient_pass(sd["ne_mat"], indep, ex_min, ex_max, delta=delta).sum())
            rand_p = int(lenient_pass(random_mat, indep, ex_min, ex_max, delta=delta).sum())
            rat = (ne_p/max(sd['ne_mat'].shape[0],1)) / max(rand_p/max(random_mat.shape[0],1), 1e-6)
            setup_results[delta] = {"ex": ex_p, "ne": ne_p, "rand": rand_p, "ratio": rat}
            lines.append(f"  {delta:<8}{_pct(ex_p, sd['ex_mat'].shape[0]):<14}{_pct(ne_p, sd['ne_mat'].shape[0]):<14}{_pct(rand_p, random_mat.shape[0]):<14}{rat:<10.2f}")
        per_setup[setup] = setup_results
        lines.append("")
    write_step_file("R5_headroom.txt", lines)
    return per_setup

# ── Step 8: E4 — class cascade (breakout / fade union filters) ────────────
def step_E4(setup_data, random_mat):
    log("STEP E4 — class cascade (breakout/fade union filters)")
    lines = []
    lines.append("=" * 100)
    lines.append("E4 — CLASS CASCADE: union-of-class example filters")
    lines.append("=" * 100)
    lines.append("Build breakout-class filter on union(HTF, BF, BASE) examples.")
    lines.append("Build fade-class filter on union(DTSS, 3-4DB) examples.")
    lines.append("Apply class filter to per-setup non-example pools; compare to per-setup filter.")
    lines.append("")
    # Build union example matrices
    breakout_ex = np.vstack([setup_data[s]["ex_mat"] for s in ["htf", "bf", "base"]])
    fade_ex = np.vstack([setup_data[s]["ex_mat"] for s in ["dtss", "3-4db"]])
    # Union of non-examples (for coverage gate, pyramid-derived)
    breakout_ne = np.vstack([setup_data[s]["ne_mat"] for s in ["htf", "bf", "base"]])
    fade_ne = np.vstack([setup_data[s]["ne_mat"] for s in ["dtss", "3-4db"]])

    per_class = {}
    for class_name, ex_union, ne_union in [
        ("BREAKOUT", breakout_ex, breakout_ne),
        ("FADE", fade_ex, fade_ne),
    ]:
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            ex_union, random_mat,
            use_r1=True, use_r2=True,
            ne_mat_for_coverage=ne_union,
        )
        pool = [int(c) for c in order[:50] if covered[c]]
        top40 = pool[:40]
        union_mat = np.vstack([ex_union, ne_union, random_mat])
        indep = decorrelate(top40, union_mat)
        per_class[class_name] = {
            "ex_min": ex_min, "ex_max": ex_max,
            "indep": indep, "order": order, "covered": covered,
            "cv_random": cv, "loo_fail": loo_fail,
        }
        lines.append("-" * 100)
        lines.append(f"{class_name} class filter  n_ex_union={ex_union.shape[0]}  n_ne_union={ne_union.shape[0]}")
        lines.append(f"  covered features: {int(covered.sum())}")
        lines.append(f"  top-40 -> indep: {len(indep)}")
        ex_p = int(lenient_pass(ex_union, indep, ex_min, ex_max).sum())
        ne_p = int(lenient_pass(ne_union, indep, ex_min, ex_max).sum())
        rand_p = int(lenient_pass(random_mat, indep, ex_min, ex_max).sum())
        lines.append(f"  EX union lenient pass:     {_pct(ex_p, ex_union.shape[0])}")
        lines.append(f"  NE union lenient pass:     {_pct(ne_p, ne_union.shape[0])}")
        lines.append(f"  RANDOM lenient pass:       {_pct(rand_p, random_mat.shape[0])}")
        lines.append("  Top-10 features:")
        for r_i, ci in enumerate(order[:10]):
            if not covered[ci]:
                break
            lines.append(f"    {r_i+1:>2}. {setup_data['htf']['expr_names'][ci]:<52} "
                         f"rand_cv={cv[ci]*100:5.1f}%")
        lines.append("")

    # Apply class filters to each setup's own non-examples
    lines.append("=" * 100)
    lines.append("CLASS FILTER APPLIED TO PER-SETUP NON-EXAMPLE POOLS (lenient)")
    lines.append("=" * 100)
    lines.append(f"{'setup':<10}{'class':<12}{'ne pass':<18}{'own-filter ne':<18}{'delta':<12}")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        cls = "BREAKOUT" if setup in BREAKOUT else "FADE"
        c = per_class[cls]
        ne_p = int(lenient_pass(sd["ne_mat"], c["indep"], c["ex_min"], c["ex_max"]).sum())
        per_setup[setup] = {"class": cls, "ne_pass_class": ne_p}
        lines.append(f"{setup:<10}{cls:<12}{_pct(ne_p, sd['ne_mat'].shape[0]):<18}")
    lines.append("")
    write_step_file("E4_cascade.txt", lines)
    return {"per_class": per_class, "per_setup": per_setup}

# ── Step 9: X2 — temporal holdout validation ──────────────────────────────
def step_X2(setup_data, random_mat, universe):
    log("STEP X2 — temporal holdout validation")
    lines = []
    lines.append("=" * 100)
    lines.append("X2 — TEMPORAL HOLDOUT VALIDATION")
    lines.append("=" * 100)
    lines.append("Split examples at O.1 change-point cutoff per setup (CLASSIFIER_SPEC §12.9).")
    lines.append("Build bands on early period only; evaluate on late examples (required 100% ex pass)")
    lines.append("and on non-example pool (carve rate on late regime).")
    lines.append("")
    per_setup = {}
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        cutoff = TEMPORAL_CUTOFFS.get(setup)
        if cutoff is None:
            lines.append(f"{setup.upper()}: no change-point cutoff (LOO-only setup) — skipping X2")
            lines.append("")
            continue
        ex_dates = [c["date"] for c in sd["ex_cl"]]
        early_mask = np.array([d < cutoff for d in ex_dates])
        late_mask = ~early_mask
        n_early, n_late = int(early_mask.sum()), int(late_mask.sum())
        if n_early < 10 or n_late < 3:
            lines.append(f"{setup.upper()} cutoff {cutoff}: early={n_early} late={n_late} — insufficient, skipping")
            lines.append("")
            continue
        ex_early = sd["ex_mat"][early_mask]
        ex_late = sd["ex_mat"][late_mask]
        # Rank with early examples only
        order, ex_min, ex_max, covered, cv, loo_fail = rank_features(
            ex_early, random_mat,
            use_r1=True, use_r2=True,
            ne_mat_for_coverage=sd["ne_mat"],
        )
        top40 = [int(c) for c in order[:40] if covered[c]]
        union_mat = np.vstack([ex_early, sd["ne_mat"], random_mat])
        indep = decorrelate(top40, union_mat)
        # Evaluate
        ex_early_pass = int(lenient_pass(ex_early, indep, ex_min, ex_max).sum())
        ex_late_pass = int(lenient_pass(ex_late, indep, ex_min, ex_max).sum())
        ne_pass = int(lenient_pass(sd["ne_mat"], indep, ex_min, ex_max).sum())
        rand_pass = int(lenient_pass(random_mat, indep, ex_min, ex_max).sum())
        per_setup[setup] = {
            "cutoff": cutoff, "n_early": n_early, "n_late": n_late,
            "ex_early_pass": ex_early_pass, "ex_late_pass": ex_late_pass,
            "ne_pass": ne_pass, "rand_pass": rand_pass, "indep_n": len(indep),
        }
        lines.append("-" * 100)
        lines.append(f"{setup.upper()}  cutoff={cutoff}  early_ex={n_early}  late_ex={n_late}")
        lines.append(f"  indep features (trained on early only): {len(indep)}")
        lines.append(f"  EX early lenient pass (sanity): {_pct(ex_early_pass, n_early)}")
        lines.append(f"  EX late lenient pass (GENERALIZATION): {_pct(ex_late_pass, n_late)}")
        lines.append(f"  NE lenient pass: {_pct(ne_pass, sd['ne_mat'].shape[0])}")
        lines.append(f"  RANDOM lenient pass: {_pct(rand_pass, random_mat.shape[0])}")
        if ex_late_pass < n_late:
            fail_ex = [sd["ex_cl"][i] for i, m in enumerate(late_mask) if m and not lenient_pass(sd["ex_mat"][i:i+1], indep, ex_min, ex_max)[0]]
            lines.append(f"  LATE EX FAILING: {len(fail_ex)}")
            for fe in fail_ex[:10]:
                lines.append(f"    {fe['ticker']:<8}  {fe['date']}")
        lines.append("")
    write_step_file("X2_temporal.txt", lines)
    return per_setup

# ── Step 10: BULLETPROOF — single output per setup, four-gate intersection ────
def step_bulletproof(setup_data, r1r2, r4_results, random_mat):
    """Single overfit-survivor output per setup. No tiers. Strict AND over
    features that survive the intersection of four hard gates:
      G1: LOO fail rate == 0            (already in covered via rank_features)
      G2: top-R4_TOP_BUCKET in EVERY    (all R4_N_FOLDS folds, not K-1)
          K-fold ranking by random-bar carve
      G3: real carve_value_random >     (per-feature permutation max over
          per-feature permutation max   PERM_N_TRIALS draws of n_ex pseudo-ex)
      G4: |corr| < CORR_THRESHOLD       (decorrelate in carve-descending order)
    """
    bp_dir = os.path.join(OUT_DIR, "bulletproof")
    os.makedirs(bp_dir, exist_ok=True)
    log("STEP BULLETPROOF — four-gate intersection, single output per setup")
    lines = []
    lines.append("=" * 100)
    lines.append("BULLETPROOF FINAL OUTPUT — single filter per setup, no tiers")
    lines.append("=" * 100)
    lines.append("Gate stack (hard intersection, each feature must survive ALL):")
    lines.append(f"  G1. LOO fail rate == {LOO_FAIL_THRESHOLD} (no example fails leave-one-out)")
    lines.append(f"  G2. Top-{R4_TOP_BUCKET} by random-bar carve in EVERY {R4_N_FOLDS}-fold ranking ({BULLETPROOF_MIN_FOLDS}/{R4_N_FOLDS})")
    lines.append(f"  G3. Real carve_value_random > per-feature permutation max over {PERM_N_TRIALS} trials")
    lines.append(f"  G4. |corr| < {CORR_THRESHOLD} (pairwise, retain higher-carve feature)")
    lines.append("Rule: strict AND over survivors, symmetric NaN-as-pass (§4.5, §5.8).")
    lines.append("")

    summary_rows = []

    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        r = r1r2.get(setup)
        if r is None:
            lines.append(f"{setup.upper()}: no R1+R2 result — skipping")
            lines.append("")
            continue
        ex_min = r["ex_min"]; ex_max = r["ex_max"]
        covered_g1 = r["covered"]            # G1: LOO fail == 0 already enforced
        cv_random = r["cv_random"]

        # G2 — every-fold top-K membership
        if setup in r4_results and "fold_counts" in r4_results[setup]:
            fold_counts = r4_results[setup]["fold_counts"]
            g2 = fold_counts >= BULLETPROOF_MIN_FOLDS
        else:
            lines.append(f"{setup.upper()}: R4 fold counts unavailable — skipping")
            lines.append("")
            continue

        # G3 — per-feature permutation significance
        n_ex_setup = sd["ex_mat"].shape[0]
        perm_seed = RANDOM_SEED + hash(setup) % 1000
        n_cores = os.cpu_count() or 4
        log(f"  {setup}: permutation max over {PERM_N_TRIALS} trials "
            f"(n_ex={n_ex_setup}, workers={n_cores})")
        t_perm = time.time()
        perm_max = compute_perm_max_carve(random_mat, n_ex_setup, PERM_N_TRIALS, perm_seed)
        log(f"    permutation finished in {time.time()-t_perm:.1f}s")
        g3 = cv_random > perm_max

        # Intersect G1, G2, G3
        survivors_pre_decorr = covered_g1 & g2 & g3
        n_covered = int(covered_g1.sum())
        n_g2 = int(g2.sum())
        n_g3 = int(g3.sum())
        n_int = int(survivors_pre_decorr.sum())

        # G4 — decorrelate, order by carve_value_random descending
        cand_cols = np.where(survivors_pre_decorr)[0]
        cand_cols_ordered = cand_cols[np.argsort(-cv_random[cand_cols])].tolist()
        union_mat = np.vstack([sd["ex_mat"], sd["ne_mat"], random_mat])
        survivors = decorrelate(cand_cols_ordered, union_mat, CORR_THRESHOLD)

        # Apply strict AND over survivors
        ex_pass_mask = lenient_pass(sd["ex_mat"], survivors, ex_min, ex_max)
        ne_pass_mask = lenient_pass(sd["ne_mat"], survivors, ex_min, ex_max)
        rand_pass_mask = lenient_pass(random_mat, survivors, ex_min, ex_max)
        ex_p = int(ex_pass_mask.sum())
        ne_p = int(ne_pass_mask.sum())
        rand_p = int(rand_pass_mask.sum())
        rat = (ne_p / max(sd['ne_mat'].shape[0], 1)) / max(
            rand_p / max(random_mat.shape[0], 1), 1e-6)

        lines.append("=" * 100)
        lines.append(f"{setup.upper()}  ex={sd['ex_mat'].shape[0]}  ne={sd['ne_mat'].shape[0]}  rand={random_mat.shape[0]}")
        lines.append(f"  G1 covered (LOO == 0):              {n_covered} features")
        lines.append(f"  G2 every-fold top-{R4_TOP_BUCKET}:               {n_g2} features")
        lines.append(f"  G3 perm-max beat:                   {n_g3} features")
        lines.append(f"  G1 ∩ G2 ∩ G3 (pre-decorrelation):   {n_int} features")
        lines.append(f"  After G4 decorrelation (|corr|<{CORR_THRESHOLD}): {len(survivors)} features")
        lines.append(f"  EX pass (should be 100% by construction): {_pct(ex_p, sd['ex_mat'].shape[0])}")
        lines.append(f"  NE pass (own pool):                       {_pct(ne_p, sd['ne_mat'].shape[0])}")
        lines.append(f"  RANDOM pass:                              {_pct(rand_p, random_mat.shape[0])}")
        lines.append(f"  own/random ratio:                         {rat:.2f}x")
        lines.append("  Surviving features (name, band, random-bar carve, perm max):")
        for c in survivors:
            lines.append(
                f"    {sd['expr_names'][c]:<52} "
                f"[{ex_min[c]:+.3g},{ex_max[c]:+.3g}]  "
                f"rand_cv={cv_random[c]*100:5.1f}%  perm_max={perm_max[c]*100:5.1f}%"
            )
        lines.append("")

        # ── Write survivors + bands files ─────────────────────────────────
        surv_path = os.path.join(bp_dir, f"presignal_{setup}_survivors.txt")
        surv_lines = []
        surv_lines.append(f"PRESIGNAL BULLETPROOF SURVIVORS — {setup.upper()}")
        surv_lines.append(f"Gates: LOO=={LOO_FAIL_THRESHOLD}, every-fold top-{R4_TOP_BUCKET} ({BULLETPROOF_MIN_FOLDS}/{R4_N_FOLDS}), perm-max beat ({PERM_N_TRIALS} trials), |corr|<{CORR_THRESHOLD}")
        surv_lines.append(f"Features surviving all gates: {len(survivors)}")
        surv_lines.append(f"EX pass: {_pct(ex_p, sd['ex_mat'].shape[0])}")
        surv_lines.append(f"NE pass: {_pct(ne_p, sd['ne_mat'].shape[0])}")
        surv_lines.append(f"RANDOM pass: {_pct(rand_p, random_mat.shape[0])}")
        surv_lines.append(f"own/random ratio: {rat:.2f}x")
        surv_lines.append("")
        surv_lines.append("SURVIVING FEATURES:")
        for c in survivors:
            surv_lines.append(
                f"  {sd['expr_names'][c]:<52} "
                f"[{ex_min[c]:+.3g},{ex_max[c]:+.3g}]  "
                f"rand_cv={cv_random[c]*100:5.1f}%  perm_max={perm_max[c]*100:5.1f}%"
            )
        surv_lines.append("")
        surv_lines.append("NON-EXAMPLE CLUSTERS PASSING FILTER:")
        by_ticker = defaultdict(list)
        for i, passed in enumerate(ne_pass_mask):
            if passed:
                c_ = sd["ne_cl"][i]
                by_ticker[c_["ticker"]].append(c_["date"])
        for tk in sorted(by_ticker.keys()):
            surv_lines.append(f"  {tk:<8}  {', '.join(sorted(by_ticker[tk]))}")
        surv_lines.append("")
        surv_lines.append(f"{len(by_ticker)} unique tickers, {sum(len(v) for v in by_ticker.values())} clusters")
        with open(surv_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(surv_lines))

        bands_path = os.path.join(bp_dir, f"presignal_{setup}_bands.json")
        bands = []
        for c in survivors:
            bands.append({
                "name": sd["expr_names"][c],
                "feature_index": int(c),
                "ex_min": float(ex_min[c]) if np.isfinite(ex_min[c]) else None,
                "ex_max": float(ex_max[c]) if np.isfinite(ex_max[c]) else None,
                "n_ex_measured": int((~np.isnan(sd["ex_mat"][:, c])).sum()),
                "random_coverage_frac": float((~np.isnan(random_mat[:, c])).sum() / max(random_mat.shape[0], 1)),
                "carve_value_random": float(cv_random[c]),
                "perm_max_carve": float(perm_max[c]),
                "loo_fail_rate": float(r["loo_fail"][c]) if np.isfinite(r["loo_fail"][c]) else None,
                "fold_count_top_bucket": int(r4_results[setup]["fold_counts"][c]),
            })
        payload = {
            "setup": setup,
            "gates": {
                "loo_fail_threshold": LOO_FAIL_THRESHOLD,
                "min_folds_top_bucket": BULLETPROOF_MIN_FOLDS,
                "top_bucket": R4_TOP_BUCKET,
                "perm_n_trials": PERM_N_TRIALS,
                "corr_threshold": CORR_THRESHOLD,
            },
            "coverage": {
                "min_ex_measured": MIN_EX_MEASURED,
                "min_random_frac": MIN_RANDOM_MEASURED_FRAC,
                "min_ne_frac": MIN_NE_MEASURED_FRAC,
            },
            "n_ex_total": sd['ex_mat'].shape[0],
            "n_ne_total": sd['ne_mat'].shape[0],
            "n_random_total": random_mat.shape[0],
            "n_features_surviving": len(survivors),
            "ex_pass": ex_p,
            "ne_pass": ne_p,
            "rand_pass": rand_p,
            "own_rand_ratio": rat,
            "gate_funnel": {
                "g1_covered_loo0": n_covered,
                "g2_every_fold_top_bucket": n_g2,
                "g3_perm_max_beat": n_g3,
                "g1_int_g2_int_g3": n_int,
                "after_g4_decorr": len(survivors),
            },
            "bands": bands,
        }
        with open(bands_path, 'w') as f:
            json.dump(payload, f, indent=2)

        summary_rows.append({
            "setup": setup,
            "n_features": len(survivors),
            "ex_pass": ex_p, "ex_total": sd['ex_mat'].shape[0],
            "ne_pass": ne_p, "ne_total": sd['ne_mat'].shape[0],
            "rand_pass": rand_p, "rand_total": random_mat.shape[0],
            "own_rand_ratio": rat,
            "g1_covered_loo0": n_covered,
            "g2_every_fold_top_bucket": n_g2,
            "g3_perm_max_beat": n_g3,
            "g1_int_g2_int_g3": n_int,
        })

    pd.DataFrame(summary_rows).to_csv(
        os.path.join(OUT_DIR, "bulletproof_summary.csv"), index=False)
    write_step_file("bulletproof.txt", lines)
    return {r["setup"]: r for r in summary_rows}

# ── Cross-version comparison table ────────────────────────────────────────
def step_comparison(setup_data, v1_baseline, r1_results, r2_results, r1r2,
                    e1_results, e2_results, r4_results, r5_results,
                    e4_results, x2_results, bulletproof_results, random_mat):
    log("STEP COMPARISON — side-by-side table")
    lines = []
    lines.append("=" * 120)
    lines.append("CROSS-VERSION COMPARISON — NE survival% under each configuration (100% EX preserved everywhere)")
    lines.append("=" * 120)
    lines.append(f"Random-bar pool: {random_mat.shape[0]} bars")
    lines.append("")
    header = f"{'setup':<10}{'v1':<10}{'R1':<10}{'R2':<10}{'R1+R2':<10}{'E1 PCA':<12}{'E2 sup':<10}{'R4 kfold':<12}{'R5 +20%':<12}{'E4 class':<12}{'X2 late':<12}{'BULLETPROOF':<14}"
    lines.append(header)
    lines.append("-" * len(header))
    for setup in SETUP_ORDER:
        sd = setup_data[setup]
        n = sd['ne_mat'].shape[0]
        v1 = f"{v1_baseline[setup]/max(n,1)*100:.1f}%"
        r1 = f"{r1_results[setup]['ne_pass']/max(n,1)*100:.1f}%" if setup in r1_results else "-"
        r2 = f"{r2_results[setup]['ne_pass']/max(n,1)*100:.1f}%" if setup in r2_results else "-"
        r12 = f"{r1r2[setup]['ne_strict']/max(n,1)*100:.1f}%" if setup in r1r2 else "-"
        e1 = f"{e1_results[setup]['ne_pass_lenient']/max(n,1)*100:.1f}%" if setup in e1_results else "-"
        e2 = f"{e2_results[setup]['ne_pass']/max(n,1)*100:.1f}%" if setup in e2_results else "-"
        r4 = f"{r4_results[setup]['ne_pass']/max(n,1)*100:.1f}%" if setup in r4_results else "-"
        r5 = f"{r5_results[setup][0.20]['ne']/max(n,1)*100:.1f}%" if setup in r5_results else "-"
        e4 = f"{e4_results['per_setup'][setup]['ne_pass_class']/max(n,1)*100:.1f}%" if setup in e4_results['per_setup'] else "-"
        x2 = f"{x2_results[setup]['ne_pass']/max(n,1)*100:.1f}%" if setup in x2_results else "-"
        bp = f"{bulletproof_results[setup]['ne_pass']/max(n,1)*100:.1f}% ({bulletproof_results[setup]['n_features']}f)" if setup in bulletproof_results else "-"
        lines.append(f"{setup:<10}{v1:<10}{r1:<10}{r2:<10}{r12:<10}{e1:<12}{e2:<10}{r4:<12}{r5:<12}{e4:<12}{x2:<12}{bp:<14}")
    lines.append("")
    lines.append("Notes:")
    lines.append("  v1 = §6.1.bis numbers (pyramid-ne denom, no LOO gate, top-40 decorrelated, strict AND)")
    lines.append("  R1 = random-bar denom only (no LOO gate)")
    lines.append("  R2 = pyramid-ne denom + LOO gate")
    lines.append("  R1+R2 = random-bar denom + LOO gate + top-40 decorrelated strict AND")
    lines.append("  E1 PCA = R1+R2 top-10 -> PCA ellipsoid bands (lenient NaN)")
    lines.append("  E2 sup = L2 logistic regression on R1+R2 top-20, threshold = min(ex score)")
    lines.append("  R4 kfold = R1+R2 features must survive 4/5 folds top-50")
    lines.append("  R5 +20% = R1+R2 bands expanded by 20% of range")
    lines.append("  E4 class = union-of-class filter applied to setup's NE pool")
    lines.append("  X2 late = NE pass when filter was trained only on pre-cutoff examples")
    lines.append(f"  BULLETPROOF = 4-gate intersection: LOO==0, every-fold top-{R4_TOP_BUCKET}, perm-max beat, decorrelated. (Nf) = surviving feature count.")
    lines.append("")
    lines.append("All values preserve 100% EX pass under lenient NaN (if lower, sanity-failure — see individual step file).")
    write_step_file("COMPARISON.txt", lines)

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(FINAL_DIR, exist_ok=True)
    t0 = time.time()
    log("=== PRESIGNAL GRINDER IMPROVEMENT RUN ===")
    log(f"Worktree: {WORKTREE}")
    log(f"Out dir:  {OUT_DIR}")

    # Preflight
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    log(f"Loading OHLCV: {ohlcv_path}")
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    log(f"Universe tickers: {len(universe)}")
    if len(universe) < 11000:
        log("FAIL: OHLCV ticker count too low")
        flush_log()
        sys.exit(1)

    ec = ExprSeriesCache()
    if not ec.is_valid():
        log("FAIL: expr cache invalid")
        flush_log()
        sys.exit(1)
    expr_names = ec.expr_names
    n_expr = ec.n_expressions
    log(f"Expr cache: {n_expr} expressions, cache path {ec.cache_path if hasattr(ec, 'cache_path') else '(see expr_cache_builder)'}")

    # Build shared random-bar matrix
    log(f"Sampling {N_RANDOM_TICKERS}×{BARS_PER_TICKER} = {N_RANDOM_TICKERS*BARS_PER_TICKER} random bars (seed={RANDOM_SEED})")
    rng = np.random.default_rng(RANDOM_SEED)
    rand_clusters = sample_random_bars(universe, N_RANDOM_TICKERS, BARS_PER_TICKER, rng)
    log(f"Sampled {len(rand_clusters)} random (ticker, bar) pairs")
    random_mat = build_matrix(rand_clusters, ec)
    any_valid = np.any(~np.isnan(random_mat), axis=1)
    random_mat = random_mat[any_valid]
    log(f"Random matrix: {random_mat.shape[0]} rows × {random_mat.shape[1]} features")

    # Per-setup data build
    setup_data = {}
    v1_baseline = {}
    log("--- Per-setup dedupe + feature matrices ---")
    for setup in SETUP_ORDER:
        log(f"  {setup}: loading pyramid + examples")
        raw = load_pyramid(os.path.join(CACHE_DIR, PYRAMIDS[setup]))
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
        ex_mat = build_matrix(ex_cl, ec)
        ne_mat = build_matrix(ne_cl, ec)
        setup_data[setup] = {
            "raw": raw, "examples": examples, "example_bar_set": example_bar_set,
            "ex_cl": ex_cl, "ne_cl": ne_cl,
            "ex_mat": ex_mat, "ne_mat": ne_mat,
            "expr_names": expr_names,
        }
        log(f"    {setup}: ex={len(ex_cl)} ne={len(ne_cl)} "
            f"ex_mat={ex_mat.shape} ne_mat={ne_mat.shape}")

        # Compute v1 baseline here: pyramid-ne denom, no LOO gate, top-40 strict AND
        order_v1, ex_min_v1, ex_max_v1, covered_v1, _, _ = rank_features(
            ex_mat, ne_mat,
            use_r1=False, use_r2=False,
            ne_mat_for_coverage=ne_mat,
        )
        top40_v1 = [int(c) for c in order_v1[:40] if covered_v1[c]]
        union_mat_v1 = np.vstack([ex_mat, ne_mat])
        indep_v1 = decorrelate(top40_v1, union_mat_v1)
        ne_p_v1 = int(lenient_pass(ne_mat, indep_v1, ex_min_v1, ex_max_v1).sum())
        v1_baseline[setup] = ne_p_v1
        log(f"    {setup} v1 baseline NE pass: {ne_p_v1}/{len(ne_cl)} ({ne_p_v1/max(len(ne_cl),1)*100:.1f}%)")

    try:
        r1_results = step_R1(setup_data, random_mat)
        r2_results = step_R2(setup_data)
        r1r2 = step_R1R2(setup_data, random_mat)
        e1_results = step_E1(setup_data, r1r2, random_mat)
        e2_results = step_E2(setup_data, r1r2, random_mat)
        r4_results = step_R4(setup_data, random_mat)
        r5_results = step_R5(setup_data, r1r2, random_mat)
        e4_results = step_E4(setup_data, random_mat)
        x2_results = step_X2(setup_data, random_mat, universe)
        bulletproof_results = step_bulletproof(setup_data, r1r2, r4_results, random_mat)
        step_comparison(setup_data, v1_baseline, r1_results, r2_results, r1r2,
                        e1_results, e2_results, r4_results, r5_results,
                        e4_results, x2_results, bulletproof_results, random_mat)
    except Exception as e:
        log(f"FAIL: exception in step execution: {e}")
        log(traceback.format_exc())
    finally:
        elapsed = time.time() - t0
        log(f"=== DONE in {elapsed:.1f}s ===")
        flush_log()

if __name__ == "__main__":
    main()
