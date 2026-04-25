"""E4 — Entry-bar anatomy per class.

For each curated example, pull the full 16,039-feature vector from the expression
cache at the entry bar (offset +1 from signal bar) and surrounding bars in the
forward window (offsets +1 through +5 from signal, so entry and 4 following bars).

For each feature, compute across pooled examples of a class:
  mean + std at each offset
  separation score: |mean(offset=+1) - mean(other_offsets)| / pooled_std
  (z-score-style, self-inferring — no designed thresholds)

Report top features by separation score per class. Also note: for curated examples
entry = signal+1 by grinder construction, so this characterizes "what does the
bar immediately after a grinder-fired signal look like, relative to bars 2-5 later?"
That tells us whether bar+1 has a detectable signature we can use for non-examples.
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
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

FADE_SETUPS = {"dtss", "3-4db"}
ACTIVE = ["htf", "bf", "base", "dtss"]
# Offsets relative to pre-entry bar (= signal bar by grinder construction).
# +1 = entry bar. +2..+5 = subsequent bars. +0 = signal bar. -1, -2 = pre-signal.
OFFSETS = [-2, -1, 0, 1, 2, 3, 4, 5]
ENTRY_OFFSET = 1


def assert_paths_safe():
    out_resolved = os.path.abspath(OUT_DIR)
    if not out_resolved.startswith(os.path.abspath(WORKTREE)):
        raise SystemExit(f"ABORT: OUT_DIR {out_resolved} outside worktree {WORKTREE}")
    print(f"  OK OUT_DIR: {out_resolved}")


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def align(df, cd):
    dates_full = ohlcv_dates_str(df)
    hits = np.where(dates_full == str(cd[0])[:10])[0]
    if len(hits) == 0:
        return None, None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != len(cd):
        return None, None
    return df_t, dates_full[off:end]


def main():
    print("=" * 60); print("E4 - Entry-bar anatomy per class"); print("=" * 60)
    assert_paths_safe()

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    with sqlite3.connect(DB) as c:
        directions = dict(c.execute("SELECT setup_type, direction FROM setups").fetchall())
        examples = [{"setup": s, "ticker": t, "entry_date": d} for s, t, d in c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss') ORDER BY setup_type, ticker, entry_date"
        ).fetchall()]

    expr = ExprSeriesCache()
    feature_names = expr.expr_names
    n_features = len(feature_names)
    print(f"  Features: {n_features:,}")

    # Per class: tensor of shape (n_examples, n_offsets, n_features)
    # For 119 examples * 8 offsets * 16039 features = 15.3M floats = ~122 MB — OK
    per_class = defaultdict(list)
    per_class_meta = defaultdict(list)

    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        df = universe.get(ticker)
        if df is None: continue
        cd, cdata = expr.get_ticker(ticker)
        if cd is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None: continue
        if entry_date not in set(dates_a): continue
        entry_idx = int(np.where(dates_a == entry_date)[0][0])
        pre_entry_idx = entry_idx - 1  # == signal bar by grinder construction
        # Need pre_entry_idx - 2 (offset -2) and entry_idx + 4 (offset +5)
        need_lo = pre_entry_idx + min(OFFSETS)
        need_hi = pre_entry_idx + max(OFFSETS)
        if need_lo < 0 or need_hi >= len(cdata):
            continue
        # Pull (n_offsets, n_features)
        rows = np.stack([cdata[pre_entry_idx + off] for off in OFFSETS], axis=0)
        per_class[klass].append(rows)
        per_class_meta[klass].append({"setup": setup, "ticker": ticker, "entry_date": entry_date})

    all_rows = []
    for klass, lst in per_class.items():
        if not lst: continue
        arr = np.stack(lst, axis=0).astype(np.float32)  # (n_ex, n_off, n_feat)
        n_ex, n_off, n_feat = arr.shape
        print(f"  {klass}: {n_ex} examples, shape={arr.shape}")

        # Per offset: mean + std across examples (ignore NaN)
        # arr[:, oi, fi] = value at offset oi for example i, feature fi
        means = np.zeros((n_off, n_feat), dtype=np.float32)
        stds = np.zeros((n_off, n_feat), dtype=np.float32)
        ns = np.zeros((n_off, n_feat), dtype=np.int32)
        for oi in range(n_off):
            x = arr[:, oi, :]
            # mean over examples ignoring NaN
            mask = ~np.isnan(x)
            ns[oi] = mask.sum(axis=0)
            means[oi] = np.where(ns[oi] > 0, np.nansum(x, axis=0) / np.maximum(ns[oi], 1), np.nan)
            # std ignoring NaN
            dev = x - means[oi]
            dev[np.isnan(dev)] = 0.0
            sq = dev * dev
            stds[oi] = np.where(ns[oi] > 1, np.sqrt(np.nansum(sq, axis=0) / np.maximum(ns[oi] - 1, 1)), np.nan)

        # Entry offset index
        entry_oi = OFFSETS.index(ENTRY_OFFSET)
        # "Other" offsets: all except entry_oi (but only +2..+5 to avoid mixing pre-signal into separation)
        other_ois = [i for i, off in enumerate(OFFSETS) if off in (2, 3, 4, 5)]

        # Separation: |entry_mean - mean(other_mean)| / pooled_std
        other_means = means[other_ois].mean(axis=0)
        pooled_std = np.sqrt(np.nanmean(stds[[entry_oi] + other_ois] ** 2, axis=0))
        sep = np.abs(means[entry_oi] - other_means) / np.where(pooled_std > 0, pooled_std, np.nan)

        # Build a DataFrame of top features
        df = pd.DataFrame({
            "feature": feature_names,
            "entry_mean": means[entry_oi],
            "entry_std": stds[entry_oi],
            "other_mean": other_means,
            "pooled_std": pooled_std,
            "sep_zscore": sep,
            "n_entry": ns[entry_oi],
        })
        df = df.dropna(subset=["sep_zscore"]).sort_values("sep_zscore", ascending=False)

        top = df.head(100).copy()
        top["class"] = klass
        all_rows.append(top)

        path = os.path.join(OUT_DIR, f"11_entry_anatomy_{klass}.csv")
        df.to_csv(path, index=False)
        print(f"  wrote {path} ({len(df)} features ranked)")

        # Summary — show top 15 per class
        print(f"  Top 15 separation features for {klass}:")
        for _, r in top.head(15).iterrows():
            print(f"    {r['feature'][:45]:<45}  sep_z={r['sep_zscore']:6.2f}  entry={r['entry_mean']:+9.3f} other={r['other_mean']:+9.3f}  pooled_std={r['pooled_std']:.3f}  n={int(r['n_entry'])}")
        print()

    if all_rows:
        combined = pd.concat(all_rows, ignore_index=True)
        combined_path = os.path.join(OUT_DIR, "11_entry_anatomy_top100_per_class.csv")
        combined.to_csv(combined_path, index=False)
        print(f"  wrote {combined_path}")


if __name__ == "__main__":
    main()
