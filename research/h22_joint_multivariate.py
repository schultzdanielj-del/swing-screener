"""H22: joint multivariate structure of the 6-MA signed-distance feature space.

Runs several analyses that pyramid's single-expression search CAN'T reach:
1. Mahalanobis distance per setup (joint-covariance-aware point distance)
2. KDE-style density score (from Mahalanobis via Gaussian kernel)
3. PCA per setup (principal components of the example cloud)
4. Correlation matrix per setup (vs. pooled)
5. Correlation-conditional residuals (linear regression residual per correlated pair)

Feature vector per example: 6 signed distances (sig_close - MA)/ADR for
{EMA3, EMA8, EMA21, SMA50, SMA200, SMA330}. Examples missing any MA dropped
from multivariate analysis.
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
    records.append({"setup": setup, "ticker": ticker, "entry": entry_date, "vec": vec})

# Drop examples with any missing MA for multivariate
clean = [r for r in records if not any(np.isnan(r["vec"]))]
dropped = [r for r in records if any(np.isnan(r["vec"]))]
print(f"Total examples: {len(records)}")
print(f"Complete-vector examples: {len(clean)} (dropped {len(dropped)} with missing MAs)")
for r in dropped:
    missing = [MA_NAMES[i] for i, v in enumerate(r["vec"]) if np.isnan(v)]
    print(f"  dropped: {r['ticker']} {r['entry']} ({r['setup']}) missing {missing}")
print()


def per_setup_matrix(setup):
    sub = [r for r in clean if r["setup"] == setup]
    X = np.array([r["vec"] for r in sub])
    labels = [(r["ticker"], r["entry"]) for r in sub]
    return X, labels


def safe_inv_cov(X):
    """Cov + pseudo-inverse for ill-conditioned 6D covariance with small n."""
    n = X.shape[0]
    mean = X.mean(axis=0)
    Xc = X - mean
    cov = (Xc.T @ Xc) / max(n - 1, 1)
    # Regularize slightly to avoid singular matrix with tiny sample
    cov_reg = cov + 1e-6 * np.eye(cov.shape[0])
    inv = np.linalg.pinv(cov_reg)
    return mean, cov, inv


def mahalanobis(x, mean, inv_cov):
    d = x - mean
    val = d @ inv_cov @ d
    return float(np.sqrt(max(val, 0)))


print("=" * 100)
print("1. MAHALANOBIS DISTANCE per setup (joint-covariance-aware distance to setup centroid)")
print("=" * 100)
setup_stats = {}
for setup in ("htf", "bf", "base"):
    X, labels = per_setup_matrix(setup)
    if len(X) < 7:
        print(f"\n{setup}: n={len(X)} too small")
        continue
    mean, cov, inv_cov = safe_inv_cov(X)
    mdists = np.array([mahalanobis(X[i], mean, inv_cov) for i in range(len(X))])
    setup_stats[setup] = {"X": X, "labels": labels, "mean": mean, "cov": cov,
                          "inv_cov": inv_cov, "mdists": mdists}
    print(f"\n{setup.upper()} (n={len(X)})")
    print(f"  centroid: " + ", ".join(f"{ma}={mean[i]:+.2f}" for i, ma in enumerate(MA_NAMES)))
    print(f"  Mahalanobis: min={mdists.min():.2f}  p50={np.median(mdists):.2f}  "
          f"p90={np.percentile(mdists, 90):.2f}  max={mdists.max():.2f}")
    # Top 3 most-outlying examples
    top3 = np.argsort(-mdists)[:3]
    print(f"  most-outlying 3:")
    for j in top3:
        print(f"    {labels[j][0]} {labels[j][1]}: M={mdists[j]:.2f}  "
              f"vec={[round(X[j,k],2) for k in range(6)]}")

print()
print("=" * 100)
print("2. CROSS-SETUP MAHALANOBIS: does each example land closest to its OWN setup?")
print("=" * 100)
# For each clean example, compute Mahalanobis dist to all 3 setup centroids
for setup in ("htf", "bf", "base"):
    wrong = 0
    total = 0
    for r in clean:
        if r["setup"] != setup: continue
        x = np.array(r["vec"])
        dists = {}
        for s2 in ("htf", "bf", "base"):
            if s2 not in setup_stats: continue
            ss = setup_stats[s2]
            dists[s2] = mahalanobis(x, ss["mean"], ss["inv_cov"])
        if not dists: continue
        closest = min(dists, key=dists.get)
        total += 1
        if closest != setup: wrong += 1
    if total > 0:
        correct = total - wrong
        print(f"  {setup}: {correct}/{total} closest to own setup ({correct/total*100:.0f}%)")

print()
print("=" * 100)
print("3. GAUSSIAN DENSITY SCORE (using Mahalanobis for density proxy)")
print("=" * 100)
print("  score = exp(-0.5 * M^2) — Gaussian-weighted density at each example point")
for setup in ("htf", "bf", "base"):
    if setup not in setup_stats: continue
    ss = setup_stats[setup]
    scores = np.exp(-0.5 * ss["mdists"]**2)
    print(f"\n{setup.upper()}:")
    print(f"  density: min={scores.min():.4f}  p50={np.median(scores):.4f}  "
          f"p90={np.percentile(scores, 90):.4f}  max={scores.max():.4f}")
    # Lowest-density examples (outliers in the setup's own cloud)
    bot3 = np.argsort(scores)[:3]
    print(f"  lowest-density 3 (most atypical examples):")
    for j in bot3:
        print(f"    {ss['labels'][j][0]} {ss['labels'][j][1]}: density={scores[j]:.4f}  "
              f"M={ss['mdists'][j]:.2f}")

print()
print("=" * 100)
print("4. PCA per setup (top principal components of example cloud)")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    if setup not in setup_stats: continue
    ss = setup_stats[setup]
    X = ss["X"]
    Xc = X - X.mean(axis=0)
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
    n = len(X)
    var_explained = (S**2) / (n - 1)
    total_var = var_explained.sum()
    pct = var_explained / total_var * 100
    cum_pct = np.cumsum(pct)
    print(f"\n{setup.upper()} (n={len(X)})")
    print(f"  variance per PC: " + " ".join(f"PC{k+1}={pct[k]:.1f}%" for k in range(len(pct))))
    print(f"  cumulative:      " + " ".join(f"     {cum_pct[k]:.1f}%" for k in range(len(cum_pct))))
    # Top-3 PC loadings
    for k in range(min(3, len(Vt))):
        loadings = Vt[k]
        print(f"  PC{k+1} loadings ({pct[k]:.1f}% var):")
        ordered = sorted(zip(MA_NAMES, loadings), key=lambda x: -abs(x[1]))
        for ma, ld in ordered:
            bar = "+" if ld > 0 else "-"
            print(f"    {bar} {ma:<8} {ld:+.3f}")

print()
print("=" * 100)
print("5. CORRELATION MATRIX per setup (vs pooled, to show setup-specific structure)")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    if setup not in setup_stats: continue
    X = setup_stats[setup]["X"]
    corr = np.corrcoef(X.T)
    print(f"\n{setup.upper()} (n={len(X)}):")
    print("         " + "  ".join(f"{m:>7}" for m in MA_NAMES))
    for i, ma in enumerate(MA_NAMES):
        print(f"  {ma:<7}" + "  ".join(f"{corr[i,j]:>7.2f}" for j in range(len(MA_NAMES))))
    # Flag strongly correlated pairs (|r| >= 0.7)
    print("  strong pairs (|r| >= 0.7):")
    for i, j in combinations(range(len(MA_NAMES)), 2):
        if abs(corr[i,j]) >= 0.7:
            print(f"    {MA_NAMES[i]:<7} vs {MA_NAMES[j]:<7}: r = {corr[i,j]:+.2f}")

print()
print("=" * 100)
print("6. CORRELATION-CONDITIONAL RESIDUALS (linear regression of strongest pair)")
print("=" * 100)
print("  for each setup, take top-2 correlated pairs, regress one MA on the other,")
print("  report residual distribution. Tight residual = strong linear joint constraint")
print("  that pyramid would miss with single-feature thresholds.")
for setup in ("htf", "bf", "base"):
    if setup not in setup_stats: continue
    X = setup_stats[setup]["X"]
    corr = np.corrcoef(X.T)
    pair_corrs = []
    for i, j in combinations(range(len(MA_NAMES)), 2):
        pair_corrs.append((i, j, corr[i,j]))
    pair_corrs.sort(key=lambda x: -abs(x[2]))
    print(f"\n{setup.upper()}:")
    for i, j, r in pair_corrs[:3]:
        # regress X[:, j] on X[:, i]:  y = beta*x + alpha; residual = y - (beta*x + alpha)
        x_col = X[:, i]; y_col = X[:, j]
        beta, alpha = np.polyfit(x_col, y_col, 1)
        pred = beta * x_col + alpha
        resid = y_col - pred
        print(f"  {MA_NAMES[j]} = {beta:+.3f}*{MA_NAMES[i]} + {alpha:+.3f}  (r={r:+.2f})")
        print(f"    residual std = {resid.std():.3f}  min={resid.min():+.3f}  max={resid.max():+.3f}")

print()
print("=" * 100)
print("7. CONVEX HULL in 2D PC projection (approximation of multi-dim shape membership)")
print("=" * 100)
try:
    from scipy.spatial import ConvexHull
    for setup in ("htf", "bf", "base"):
        if setup not in setup_stats: continue
        X = setup_stats[setup]["X"]
        Xc = X - X.mean(axis=0)
        U, S, Vt = np.linalg.svd(Xc, full_matrices=False)
        # Project onto top 2 PCs
        Xp = Xc @ Vt[:2].T  # n x 2
        try:
            hull = ConvexHull(Xp)
            pct = (S[:2]**2).sum() / (S**2).sum() * 100
            print(f"\n{setup.upper()} (n={len(X)}, {pct:.1f}% variance in 2D)")
            print(f"  hull has {len(hull.vertices)} boundary points")
            print(f"  PC1 range: [{Xp[:,0].min():+.2f}, {Xp[:,0].max():+.2f}]")
            print(f"  PC2 range: [{Xp[:,1].min():+.2f}, {Xp[:,1].max():+.2f}]")
            # Check self-containment: every example inside hull? (should be by construction)
        except Exception as e:
            print(f"  {setup}: hull failed ({e})")
except ImportError:
    print("  scipy not available — skipping convex hull")
