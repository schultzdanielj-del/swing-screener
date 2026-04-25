"""H24: apply h23's per-setup Mahalanobis model to the wild signal pool.

Honest mode: no threshold is picked by the code. Output shows each wild's
Mahalanobis distance to each setup centroid + whether it's inside the
example envelope (<=max observed) for its nearest setup.

Pool source: research/classifier_pool/{htf,bf,base}_pool.json
(pre_dedup breakout clusters per project_entry_detector_build_plan memory).
"""
import json
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
POOL_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier/research/classifier_pool"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 200), ("SMA", 330)]
MA_NAMES = [f"{k}{n}" for k, n in MA_SET]
D = len(MA_NAMES)

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with sqlite3.connect(DB) as conn:
    ex_rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples "
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


def feature_vector(ticker, sig_idx):
    """Return signed-distance vector (length 6) or None if ADR invalid."""
    d, h, l, c = load(ticker)
    if sig_idx < 14 or sig_idx >= len(c): return None
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: return None
    sc = c[sig_idx]
    vec = np.full(D, np.nan)
    for k, (kind, n) in enumerate(MA_SET):
        if sig_idx < n-1: continue
        v = sma(c, n, sig_idx) if kind=="SMA" else ema(c, n, sig_idx)
        if np.isfinite(v):
            vec[k] = (sc - v) / adr
    return vec


# --- build example-based models (same as h23) ---
ex_records = []
for setup, ticker, entry_date in ex_rows:
    if ticker not in universe: continue
    d, _, _, _ = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    sig_idx = int(hits[0]) - 1
    vec = feature_vector(ticker, sig_idx)
    if vec is None: continue
    ex_records.append({"setup": setup, "ticker": ticker, "entry": entry_date, "vec": vec})

print(f"Example records: {len(ex_records)}")

setup_model = {}
for setup in ("htf", "bf", "base"):
    sub = [r for r in ex_records if r["setup"] == setup]
    mean = np.full(D, np.nan)
    cov = np.full((D, D), np.nan)
    for i in range(D):
        vals = np.array([r["vec"][i] for r in sub if not np.isnan(r["vec"][i])])
        if len(vals) >= 2: mean[i] = vals.mean()
    for i in range(D):
        for j in range(D):
            pairs = np.array([(r["vec"][i], r["vec"][j]) for r in sub
                              if not np.isnan(r["vec"][i]) and not np.isnan(r["vec"][j])])
            if len(pairs) >= 2:
                xi = pairs[:,0] - pairs[:,0].mean()
                xj = pairs[:,1] - pairs[:,1].mean()
                cov[i,j] = float((xi * xj).sum() / (len(pairs) - 1))
    setup_model[setup] = {"mean": mean, "cov": cov, "sub": sub}


def mahalanobis_reduced(x, mean, cov):
    avail = [k for k in range(D) if not np.isnan(x[k])]
    if len(avail) < 2: return None
    x_sub = x[avail]; mu_sub = mean[avail]
    if np.any(np.isnan(mu_sub)): return None
    sub_cov = cov[np.ix_(avail, avail)] + 1e-6 * np.eye(len(avail))
    inv = np.linalg.pinv(sub_cov)
    d = x_sub - mu_sub
    m2 = float(d @ inv @ d)
    return float(np.sqrt(max(m2, 0))), len(avail)


# --- compute example Mahalanobis (for envelope boundary) ---
example_max = {}
example_dist = {}
for setup in ("htf", "bf", "base"):
    mdls = setup_model[setup]
    dists = []
    for r in mdls["sub"]:
        res = mahalanobis_reduced(r["vec"], mdls["mean"], mdls["cov"])
        if res: dists.append(res[0])
    example_max[setup] = max(dists) if dists else None
    example_dist[setup] = dists

print("\nExample envelope boundaries (max observed Mahalanobis per setup):")
for s in ("htf", "bf", "base"):
    print(f"  {s}: max={example_max[s]:.3f}")

# --- load wild pool ---
wild_signals = {}  # setup -> list of cluster dicts
for setup in ("htf", "bf", "base"):
    path = f"{POOL_DIR}/{setup}_pool.json"
    with open(path, "r") as f:
        pool_json = json.load(f)
    clusters = pool_json.get("clusters", [])
    wild_signals[setup] = clusters
    print(f"\n{setup} pool: total={len(clusters)}  examples={sum(1 for c in clusters if c.get('is_example'))}")

# --- compute Mahalanobis for every cluster (example + wild combined in pool) ---
print()
print("=" * 100)
print("WILD POOL MAHALANOBIS RESULTS")
print("=" * 100)

ex_lookup = {(r["ticker"], r["entry"]): True for r in ex_records}

pool_records = {}
for setup in ("htf", "bf", "base"):
    out = []
    for cluster in wild_signals[setup]:
        ticker = cluster.get("ticker")
        rightmost = cluster.get("rightmost", {})
        sig_idx = rightmost.get("bar_idx")
        sig_date = rightmost.get("date")
        is_ex = bool(cluster.get("is_example"))
        entry_date = cluster.get("example_entry_date")
        if ticker is None or sig_idx is None: continue
        if ticker not in universe: continue
        vec = feature_vector(ticker, int(sig_idx))
        if vec is None: continue
        # Mahalanobis vs each setup
        dists = {}
        for s2 in ("htf", "bf", "base"):
            res = mahalanobis_reduced(vec, setup_model[s2]["mean"], setup_model[s2]["cov"])
            if res: dists[s2] = res[0]
        if not dists: continue
        nearest = min(dists, key=dists.get)
        out.append({
            "ticker": ticker, "sig_date": sig_date, "entry_date": entry_date,
            "setup_labeled": setup, "is_example": is_ex,
            "dist_htf": dists.get("htf"), "dist_bf": dists.get("bf"),
            "dist_base": dists.get("base"), "nearest": nearest,
        })
    pool_records[setup] = out
    print(f"\n{setup.upper()} pool (labeled): {len(out)} signals scored")

# --- per-setup wild-only stats (exclude examples) ---
for setup in ("htf", "bf", "base"):
    pool = pool_records[setup]
    wild_only = [p for p in pool if not p["is_example"]]
    ex_in_pool = [p for p in pool if p["is_example"]]

    ex_max = example_max[setup]
    print()
    print("=" * 100)
    print(f"{setup.upper()}  — pool size: {len(pool)}  (examples: {len(ex_in_pool)}, wild: {len(wild_only)})")
    print(f"Example envelope: max Mahalanobis = {ex_max:.3f}")
    print("=" * 100)

    # Distribution of wild Mahalanobis (vs labeled setup)
    wild_m = np.array([p[f"dist_{setup}"] for p in wild_only
                       if p[f"dist_{setup}"] is not None])
    if len(wild_m) > 0:
        print(f"\n  Wild Mahalanobis to labeled setup '{setup}' (n={len(wild_m)}):")
        print(f"    min={wild_m.min():.3f}  p25={np.percentile(wild_m,25):.3f}  "
              f"p50={np.percentile(wild_m,50):.3f}  p75={np.percentile(wild_m,75):.3f}  "
              f"p90={np.percentile(wild_m,90):.3f}  max={wild_m.max():.3f}")
        inside_env = int(np.sum(wild_m <= ex_max))
        outside_env = int(np.sum(wild_m > ex_max))
        print(f"    inside example envelope (<= {ex_max:.2f}): {inside_env}/{len(wild_m)} "
              f"({inside_env/len(wild_m)*100:.1f}%)")
        print(f"    outside envelope (> {ex_max:.2f}): {outside_env}/{len(wild_m)} "
              f"({outside_env/len(wild_m)*100:.1f}%)")

    # Nearest-setup classification of wild
    near_counts = Counter(p["nearest"] for p in wild_only)
    print(f"\n  Wild nearest-setup classification:")
    for s2 in ("htf", "bf", "base"):
        print(f"    nearest to {s2}: {near_counts[s2]}/{len(wild_only)} "
              f"({near_counts[s2]/max(len(wild_only),1)*100:.1f}%)")

    # Histogram bins for wild Mahalanobis vs labeled setup
    if len(wild_m) > 0:
        print(f"\n  Wild Mahalanobis histogram (bins of 0.5):")
        bins = np.arange(0, max(wild_m.max(), ex_max) + 1, 0.5)
        hist, edges = np.histogram(wild_m, bins=bins)
        for k in range(len(hist)):
            marker = "  <-- ex_max" if edges[k] <= ex_max < edges[k+1] else ""
            bar = "#" * int(hist[k] / max(hist.max(), 1) * 40)
            print(f"    [{edges[k]:4.1f}, {edges[k+1]:4.1f})  {hist[k]:>4}  {bar}{marker}")

print()
print("=" * 100)
print("SUMMARY (wild-only, combined)")
print("=" * 100)
total_inside = 0; total_wild = 0
for setup in ("htf", "bf", "base"):
    pool = pool_records[setup]
    wild_only = [p for p in pool if not p["is_example"]]
    ex_max = example_max[setup]
    wild_m = np.array([p[f"dist_{setup}"] for p in wild_only
                       if p[f"dist_{setup}"] is not None])
    inside = int(np.sum(wild_m <= ex_max)) if len(wild_m) else 0
    total_inside += inside
    total_wild += len(wild_m)
    print(f"  {setup:<5}  {inside:>4}/{len(wild_m):<4} inside envelope ({inside/max(len(wild_m),1)*100:.1f}%)  "
          f"envelope={ex_max:.2f}")
print(f"\n  TOTAL  {total_inside}/{total_wild}  ({total_inside/max(total_wild,1)*100:.1f}%) "
      f"of wild signals inside their labeled setup's example envelope")
