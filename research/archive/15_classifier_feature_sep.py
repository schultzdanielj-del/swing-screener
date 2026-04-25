"""E10 — Per-setup signal-bar feature separation: WIN vs LOSS.

For each setup, take the signal-bar feature vector from the expression cache
for every non-example raced signal in E6's labels CSV. Split into WIN and LOSS
groups (drop BE — ambiguous for classification). Per feature:
  separation z = |mean_WIN - mean_LOSS| / pooled_std

Output top-50 features per setup + an overlap matrix: how many of the top-50
features overlap across setups? If high overlap -> universal feature set
possible. If low overlap -> per-setup rules are warranted.
"""
from __future__ import annotations

import os, sys, pickle, sqlite3
from collections import defaultdict
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache  # noqa: E402

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

ACTIVE = ["htf", "bf", "base", "dtss"]
TOP_N = 50


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def align(df, cd):
    dates_full = ohlcv_dates_str(df)
    hits = np.where(dates_full == str(cd[0])[:10])[0]
    if len(hits) == 0: return None, None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != len(cd): return None, None
    return df_t, dates_full[off:end]


def main():
    print("=" * 60); print("E10 — Per-setup feature separation WIN vs LOSS"); print("=" * 60)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    expr = ExprSeriesCache()
    feature_names = expr.expr_names
    n_features = len(feature_names)
    print(f"  Features: {n_features:,}")

    per_setup_top = {}
    per_setup_rankings = {}

    for setup in ACTIVE:
        print(f"\n---- {setup.upper()} ----")
        labels = pd.read_csv(os.path.join(OUT_DIR, f"13_population_labels_{setup}.csv"))
        # non-example raced WIN and LOSS only (drop BE, drop is_example, drop null outcomes)
        nex = labels[(labels.is_example == 0) & labels.outcome.notna()]
        wins = nex[nex.outcome == "WIN"]
        losses = nex[nex.outcome == "LOSS"]
        print(f"  WIN={len(wins)}  LOSS={len(losses)}")

        # Collect signal-bar feature vectors
        def gather(rows):
            vecs = []
            tcache = {}
            for _, r in rows.iterrows():
                tk = r["ticker"]; sig_idx = int(r["signal_idx"])
                if tk not in tcache:
                    df = universe.get(tk)
                    if df is None: tcache[tk] = None; continue
                    cd, cdata = expr.get_ticker(tk)
                    if cd is None: tcache[tk] = None; continue
                    df_a, dates_a = align(df, cd)
                    if df_a is None: tcache[tk] = None; continue
                    tcache[tk] = cdata
                cdata = tcache.get(tk)
                if cdata is None: continue
                if sig_idx >= len(cdata) or sig_idx < 0: continue
                vecs.append(cdata[sig_idx])
            return np.stack(vecs, axis=0).astype(np.float32) if vecs else None

        X_w = gather(wins)
        X_l = gather(losses)
        if X_w is None or X_l is None:
            print(f"  SKIP: missing feature vectors")
            continue
        print(f"  gathered shapes: WIN {X_w.shape}, LOSS {X_l.shape}")

        # Per feature: mean + std across examples, ignoring NaN
        def mean_std(X):
            mask = ~np.isnan(X)
            n = mask.sum(axis=0).astype(np.float32)
            m = np.where(n > 0, np.nansum(X, axis=0) / np.maximum(n, 1), np.nan)
            dev = X - m
            dev[np.isnan(dev)] = 0.0
            v = np.nansum(dev * dev, axis=0) / np.maximum(n - 1, 1)
            s = np.where(n > 1, np.sqrt(v), np.nan)
            return m, s, n

        m_w, s_w, n_w = mean_std(X_w)
        m_l, s_l, n_l = mean_std(X_l)
        # Pooled std
        pooled = np.sqrt((s_w ** 2 + s_l ** 2) / 2.0)
        sep = np.abs(m_w - m_l) / np.where(pooled > 0, pooled, np.nan)

        df_sep = pd.DataFrame({
            "feature": feature_names,
            "mean_WIN": m_w, "mean_LOSS": m_l,
            "std_WIN": s_w, "std_LOSS": s_l,
            "n_WIN": n_w.astype(int), "n_LOSS": n_l.astype(int),
            "sep_z": sep,
        }).dropna(subset=["sep_z"]).sort_values("sep_z", ascending=False)

        path = os.path.join(OUT_DIR, f"15_feature_sep_{setup}.csv")
        df_sep.to_csv(path, index=False)
        print(f"  wrote {path}")

        top = df_sep.head(TOP_N)
        per_setup_top[setup] = set(top["feature"].tolist())
        per_setup_rankings[setup] = top

        print(f"  Top 15 features for {setup} by WIN-vs-LOSS separation:")
        for _, r in top.head(15).iterrows():
            print(f"    {r['feature'][:45]:<45}  sep_z={r['sep_z']:5.2f}  WIN={r['mean_WIN']:+9.3f}  LOSS={r['mean_LOSS']:+9.3f}")

    # Overlap matrix
    print()
    print("=" * 60)
    print(f"OVERLAP: top-{TOP_N} features shared between setups")
    print("=" * 60)
    setups = list(per_setup_top.keys())
    header = "       " + "  ".join(f"{s:>5}" for s in setups)
    print(header)
    for s1 in setups:
        row = [f"{s1:>5}: "]
        for s2 in setups:
            if s1 == s2:
                row.append(f"{TOP_N:>5}")
            else:
                shared = len(per_setup_top[s1] & per_setup_top[s2])
                row.append(f"{shared:>5}")
        print("  ".join(row))

    # Any feature that's in the top-50 of ALL 4 setups
    universal = set.intersection(*per_setup_top.values()) if per_setup_top else set()
    print()
    print(f"Features in top-{TOP_N} of ALL 4 setups: {len(universal)}")
    if universal:
        for f in sorted(universal):
            # average sep_z across setups for this feature
            avg_sep = np.mean([per_setup_rankings[s].set_index("feature").loc[f, "sep_z"] for s in setups])
            print(f"  {f}  avg_sep_z={avg_sep:.2f}")
    print()

    # Setup-unique features (in top-50 of only ONE setup)
    print(f"Features unique to a single setup's top-{TOP_N}:")
    for s in setups:
        others = set.union(*(per_setup_top[o] for o in setups if o != s))
        unique = per_setup_top[s] - others
        print(f"  {s}: {len(unique)} unique features (out of {TOP_N})")


if __name__ == "__main__":
    main()
