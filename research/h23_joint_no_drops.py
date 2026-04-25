"""H23: joint multivariate structure, NO examples dropped for missing MAs.

Corrects h22's wrongful drop of 8 examples. Per Dan 2026-04-23: missing MAs
don't alter the data. Each example is analyzed using whatever dimensions it has.

Method:
- Per-MA centroid: computed over all examples with that MA
- Pairwise covariance: Cov(MA_i, MA_j) computed over examples that have both
- Per-example Mahalanobis: reduced to the example's available dimensions, using
  the sub-covariance indexed by those dims
- Values reported with dim count so they're comparable
- QXO 2025-06-06 kept in — if it's a data bug we address it separately
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from itertools import combinations

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 200), ("SMA", 330)]
MA_NAMES = [f"{k}{n}" for k, n in MA_SET]
D = len(MA_NAMES)

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples "
                        "WHERE setup_type IN ('htf','bf','base') "
                        "ORDER BY setup_type, ticker, entry_date").fetchall()


def load(t):
    df = universe[t]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy(); df["date"] = pd.to_datetime(df["date"])
    return (df["date"].dt.strftime("%Y-%m-%d").values,
            df["high"].values.astype(float), df["low"].values.astype(float),
            df["close"].values.astype(float))


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")
def sma(a, n, i): return float(np.mean(a[i-n+1:i+1])) if i >= n-1 else float("nan")
def ema(a, n, i):
    if i < n-1: return float("nan")
    alpha = 2.0/(n+1); e = a[0]
    for k in range(1, i+1): e = alpha*a[k] + (1-alpha)*e
    return float(e)


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue
    sc = c[i]
    vec = []
    for kind, n in MA_SET:
        if i < n-1:
            vec.append(float("nan")); continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        vec.append((sc - v)/adr if np.isfinite(v) else float("nan"))
    records.append({"setup": setup, "ticker": ticker, "entry": entry_date, "vec": np.array(vec)})

print(f"All examples kept: {len(records)}")
print()


def per_setup_marginal_stats(setup):
    """Per-MA: mean and std using all examples with that MA available."""
    sub = [r for r in records if r["setup"] == setup]
    mean = np.full(D, np.nan)
    std = np.full(D, np.nan)
    n_per_dim = np.zeros(D, dtype=int)
    for k in range(D):
        vals = np.array([r["vec"][k] for r in sub if not np.isnan(r["vec"][k])])
        n_per_dim[k] = len(vals)
        if len(vals) >= 2:
            mean[k] = vals.mean()
            std[k] = vals.std(ddof=1)
    return mean, std, n_per_dim, sub


def per_setup_pairwise_cov(setup):
    """Covariance entry (i,j) computed over examples that have BOTH MAs."""
    sub = [r for r in records if r["setup"] == setup]
    mean = np.full(D, np.nan)
    cov = np.full((D, D), np.nan)
    n_ij = np.zeros((D, D), dtype=int)
    # Per-dim mean
    for k in range(D):
        vals = np.array([r["vec"][k] for r in sub if not np.isnan(r["vec"][k])])
        if len(vals) >= 2: mean[k] = vals.mean()
    # Pairwise cov
    for i in range(D):
        for j in range(D):
            pairs = np.array([(r["vec"][i], r["vec"][j]) for r in sub
                              if not np.isnan(r["vec"][i]) and not np.isnan(r["vec"][j])])
            n_ij[i,j] = len(pairs)
            if len(pairs) >= 2:
                xi = pairs[:,0] - pairs[:,0].mean()
                xj = pairs[:,1] - pairs[:,1].mean()
                cov[i,j] = float((xi * xj).sum() / (len(pairs) - 1))
    return mean, cov, n_ij, sub


def safe_inv_sub(cov_full, indices):
    """Extract submatrix of cov_full at indices and pseudo-invert with regularization."""
    sub = cov_full[np.ix_(indices, indices)]
    reg = sub + 1e-6 * np.eye(len(indices))
    return np.linalg.pinv(reg)


def per_example_mahalanobis(r, mean, cov_full):
    """Compute Mahalanobis using only the dims available for this example."""
    avail = [k for k in range(D) if not np.isnan(r["vec"][k])]
    if len(avail) < 2: return None, len(avail)
    x_sub = r["vec"][avail]
    mu_sub = mean[avail]
    # Check any NaN in mean (shouldn't happen if setup has any examples)
    if np.any(np.isnan(mu_sub)):
        return None, len(avail)
    inv = safe_inv_sub(cov_full, avail)
    d = x_sub - mu_sub
    m2 = float(d @ inv @ d)
    return float(np.sqrt(max(m2, 0))), len(avail)


print("=" * 100)
print("1. MARGINAL STATS per MA per setup (n shows data coverage)")
print("=" * 100)
setup_cov = {}
for setup in ("htf", "bf", "base"):
    mean, cov, n_ij, sub = per_setup_pairwise_cov(setup)
    setup_cov[setup] = {"mean": mean, "cov": cov, "n_ij": n_ij, "sub": sub}
    print(f"\n{setup.upper()}  total examples={len(sub)}")
    print(f"  {'MA':<8} {'n':>4} {'mean':>8} {'std':>7}")
    for k, ma in enumerate(MA_NAMES):
        vals = np.array([r["vec"][k] for r in sub if not np.isnan(r["vec"][k])])
        n = len(vals)
        if n >= 2:
            print(f"  {ma:<8} {n:>4} {vals.mean():>+8.3f} {vals.std(ddof=1):>7.3f}")
        else:
            print(f"  {ma:<8} {n:>4}   (insufficient)")

print()
print("=" * 100)
print("2. PER-EXAMPLE MAHALANOBIS (reduced dim per example, all 110 kept)")
print("=" * 100)
mahals = {}
for setup in ("htf", "bf", "base"):
    if setup not in setup_cov: continue
    sc = setup_cov[setup]
    mdists_with_dims = []
    for r in sc["sub"]:
        m, k = per_example_mahalanobis(r, sc["mean"], sc["cov"])
        if m is not None:
            mdists_with_dims.append((r["ticker"], r["entry"], m, k))
    mahals[setup] = mdists_with_dims
    ms = np.array([x[2] for x in mdists_with_dims])
    print(f"\n{setup.upper()}  n_evaluable={len(mdists_with_dims)}/{len(sc['sub'])}")
    print(f"  Mahalanobis: min={ms.min():.2f}  p50={np.median(ms):.2f}  "
          f"p75={np.percentile(ms, 75):.2f}  p90={np.percentile(ms, 90):.2f}  max={ms.max():.2f}")
    # Top 5 outliers
    top5 = sorted(mdists_with_dims, key=lambda x: -x[2])[:5]
    print(f"  most-outlying 5:")
    for tk, en, m, k in top5:
        print(f"    {tk:<6} {en}  M={m:.2f}  dims_used={k}")
    # Dim breakdown
    from collections import Counter
    dim_counts = Counter(x[3] for x in mdists_with_dims)
    print(f"  dim coverage: " + ", ".join(f"{k}D:{c}" for k, c in sorted(dim_counts.items())))

print()
print("=" * 100)
print("3. CROSS-SETUP MAHALANOBIS (each example vs. all 3 setup centroids)")
print("=" * 100)
print("  tests whether examples land closest to their own setup")
for setup in ("htf", "bf", "base"):
    if setup not in setup_cov: continue
    correct = 0; total = 0
    for r in setup_cov[setup]["sub"]:
        dists = {}
        for s2 in ("htf", "bf", "base"):
            if s2 not in setup_cov: continue
            sc2 = setup_cov[s2]
            m, _ = per_example_mahalanobis(r, sc2["mean"], sc2["cov"])
            if m is not None:
                dists[s2] = m
        if not dists: continue
        closest = min(dists, key=dists.get)
        total += 1
        if closest == setup: correct += 1
    if total > 0:
        print(f"  {setup}: {correct}/{total} closest to own setup ({correct/total*100:.0f}%)")

print()
print("=" * 100)
print("4. PAIRWISE CORRELATION per setup (pairwise-complete, outlier-sensitive)")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    if setup not in setup_cov: continue
    sc = setup_cov[setup]
    cov = sc["cov"]
    # Build correlation matrix
    std = np.sqrt(np.diag(cov))
    corr = np.full((D, D), np.nan)
    for i in range(D):
        for j in range(D):
            if std[i] > 0 and std[j] > 0 and not np.isnan(cov[i,j]):
                corr[i,j] = cov[i,j] / (std[i] * std[j])
    print(f"\n{setup.upper()}:")
    print("         " + "  ".join(f"{m:>7}" for m in MA_NAMES))
    for i, ma in enumerate(MA_NAMES):
        row = "  ".join(f"{corr[i,j]:>+7.2f}" if not np.isnan(corr[i,j]) else "    n/a"
                       for j in range(D))
        print(f"  {ma:<7}{row}")
    strong = [(i,j,corr[i,j]) for i,j in combinations(range(D), 2)
              if not np.isnan(corr[i,j]) and abs(corr[i,j]) >= 0.7]
    print(f"  strong pairs (|r| >= 0.7, n>=2):")
    for i, j, r in strong:
        print(f"    {MA_NAMES[i]:<7} vs {MA_NAMES[j]:<7}: r={r:+.2f}  "
              f"n_pair={sc['n_ij'][i,j]}")

print()
print("=" * 100)
print("5. PCA on MARGINALLY-COMPLETE dims (dims that ALL examples have)")
print("=" * 100)
# Find dims that every example has
for setup in ("htf", "bf", "base"):
    if setup not in setup_cov: continue
    sub = setup_cov[setup]["sub"]
    # For each dim, is it present in ALL examples?
    full_dims = [k for k in range(D)
                 if all(not np.isnan(r["vec"][k]) for r in sub)]
    print(f"\n{setup.upper()}  dims present in ALL {len(sub)} examples: "
          f"{[MA_NAMES[k] for k in full_dims]}")
    if len(full_dims) < 2: continue
    X = np.array([[r["vec"][k] for k in full_dims] for r in sub])
    Xc = X - X.mean(axis=0)
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    var = S**2 / (len(X) - 1)
    pct = var / var.sum() * 100
    print(f"  variance per PC: " + " ".join(f"PC{k+1}={pct[k]:.1f}%" for k in range(len(pct))))
    for k in range(min(3, len(Vt))):
        ordered = sorted(zip([MA_NAMES[full_dims[j]] for j in range(len(full_dims))], Vt[k]),
                         key=lambda x: -abs(x[1]))
        line = ", ".join(f"{ma}={ld:+.2f}" for ma, ld in ordered)
        print(f"  PC{k+1} ({pct[k]:.1f}%): {line}")

print()
print("=" * 100)
print("6. LINEAR RIDGE RESIDUALS (strongest pair per setup, pairwise-complete data)")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    if setup not in setup_cov: continue
    sc = setup_cov[setup]
    cov = sc["cov"]
    std = np.sqrt(np.diag(cov))
    corrs = []
    for i, j in combinations(range(D), 2):
        if std[i] > 0 and std[j] > 0 and not np.isnan(cov[i,j]):
            corrs.append((i, j, cov[i,j] / (std[i] * std[j])))
    corrs.sort(key=lambda x: -abs(x[2]))
    print(f"\n{setup.upper()}  (top 3 correlated pairs)")
    for i, j, r in corrs[:3]:
        # Pairwise-complete regression
        pairs = np.array([(rr["vec"][i], rr["vec"][j]) for rr in sc["sub"]
                          if not np.isnan(rr["vec"][i]) and not np.isnan(rr["vec"][j])])
        if len(pairs) < 3: continue
        x_col, y_col = pairs[:,0], pairs[:,1]
        beta, alpha = np.polyfit(x_col, y_col, 1)
        resid = y_col - (beta * x_col + alpha)
        print(f"  {MA_NAMES[j]} = {beta:+.3f}*{MA_NAMES[i]} + {alpha:+.3f}  "
              f"(r={r:+.2f}, n={len(pairs)})")
        print(f"    residual std = {resid.std():.3f}  min={resid.min():+.3f}  max={resid.max():+.3f}")
