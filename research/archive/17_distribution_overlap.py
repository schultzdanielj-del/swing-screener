"""Deep winner-vs-loser distribution + per-setup breakdown.

E16 gave median diffs + z-score separation but not distribution shape. A median
diff of +0.14 could mean a clean threshold or heavy overlap with a tail shift.
This script quantifies the overlap.

For the top N features per class from E16 + the bar-shape metrics, extract the
raw value at every firing bar. Compute per-setup and per-class:
  - winner quantiles (p10/p25/p50/p75/p90)
  - loser quantiles (same)
  - overlap metric: where does winner p50 land in loser's distribution (as quantile)
    and vice versa. Clean separation = winner p50 at loser p80+ (or lower p20-).
  - AUC-style quantile-gap: fraction of (winner, loser) pairs where winner > loser.
    0.5 = no separation; 1.0 = perfect; <0.5 = winner lower than loser.

Memory-safe: process ticker by ticker, keep only TOP-N feature columns + shape.
~30 features × 3000 bars × 4 bytes = 360 KB.

Output:
  research/out/17_overlap_per_class.csv — top features, per-class + per-setup stats
  research/out/17_overlap_shape.csv — bar-shape metrics per class + per setup
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3, gc
from collections import defaultdict
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

FADE_SETUPS = {"dtss", "3-4db"}
ACTIVE = ["htf", "bf", "base", "dtss"]
TOP_N_FEATURES = 30


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


def latest_pyramid(setup):
    fs = glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json"))
    fs = [f for f in fs if "sig0_pk0" not in os.path.basename(f)]
    wt = [(json.load(open(f))["timestamp"], f) for f in fs]
    wt.sort()
    return wt[-1][1] if wt else None


def final_signals_from_pyramid(pyr):
    out = []
    for _, tr in pyr["tier_results"].items():
        if tr.get("final_signals"):
            out = tr["final_signals"]
    return out


def auc_like(w_vals, l_vals):
    """Fraction of (w, l) pairs where w > l. 0.5 = no separation, 1.0 = perfect,
    <0.5 = winner lower than loser. Computed via ranks (Mann-Whitney U equivalent)."""
    w = np.asarray(w_vals, dtype=float); w = w[np.isfinite(w)]
    l = np.asarray(l_vals, dtype=float); l = l[np.isfinite(l)]
    if len(w) == 0 or len(l) == 0: return np.nan
    combined = np.concatenate([w, l])
    ranks = pd.Series(combined).rank(method="average").values
    w_ranks = ranks[:len(w)]
    n_w = len(w); n_l = len(l)
    U = w_ranks.sum() - n_w * (n_w + 1) / 2.0
    return float(U / (n_w * n_l))


def winner_p50_in_loser_distribution(w_vals, l_vals):
    """What quantile of loser's distribution does winner's median sit at?"""
    w = np.asarray(w_vals, dtype=float); w = w[np.isfinite(w)]
    l = np.asarray(l_vals, dtype=float); l = l[np.isfinite(l)]
    if len(w) == 0 or len(l) == 0: return np.nan
    wmed = np.median(w)
    # What fraction of losers are <= wmed?
    return float(np.mean(l <= wmed))


def main():
    print("=" * 70); print("Distribution overlap + per-setup breakdown"); print("=" * 70)

    # Load top-N features per class from E16
    e16_br = pd.read_csv(os.path.join(OUT_DIR, "16_winners_vs_losers_breakout.csv"))
    e16_fd = pd.read_csv(os.path.join(OUT_DIR, "16_winners_vs_losers_fade.csv"))
    top_br = e16_br.head(TOP_N_FEATURES)["feature"].tolist()
    top_fd = e16_fd.head(TOP_N_FEATURES)["feature"].tolist()
    # Union of features we need to pull
    feature_union = list(dict.fromkeys(top_br + top_fd))
    print(f"  top breakout: {len(top_br)}  top fade: {len(top_fd)}  union: {len(feature_union)}")

    # Map feature name -> column index
    expr = ExprSeriesCache()
    all_names = expr.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}
    feature_cols = [name_to_idx.get(n) for n in feature_union]
    if any(c is None for c in feature_cols):
        missing = [n for n, c in zip(feature_union, feature_cols) if c is None]
        print(f"  ERROR: missing feature columns: {missing}"); return
    feature_cols_arr = np.array(feature_cols, dtype=int)
    adr_col = expr.expr_index("adr14")

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  OHLCV tickers: {len(universe):,}")

    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
    examples_by_setup = defaultdict(list)
    for s, t, d in ex_rows:
        examples_by_setup[s].append((t, d))

    # Build clusters per setup (same as E16)
    all_clusters = []
    ticker_to_cluster_ids = defaultdict(list)
    ticker_alignment = {}

    for setup in ACTIVE:
        pyr_path = latest_pyramid(setup)
        if pyr_path is None: continue
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        per_tk = defaultdict(list)
        for s in sigs: per_tk[s["ticker"]].append(s["date"])

        for tk, sdates in per_tk.items():
            if tk not in ticker_alignment:
                df = universe.get(tk)
                if df is None: ticker_alignment[tk] = None; continue
                cd, cdata = expr.get_ticker(tk)
                cdata = None; gc.collect()
                if cd is None: ticker_alignment[tk] = None; continue
                df_a, dates_a = align(df, cd)
                if df_a is None: ticker_alignment[tk] = None; continue
                ticker_alignment[tk] = (df_a, dates_a)
            if ticker_alignment[tk] is None: continue
            df_a, dates_a = ticker_alignment[tk]
            bidxs = []
            for sd in sdates:
                hits = np.where(dates_a == sd)[0]
                if len(hits) == 0: continue
                bidxs.append(int(hits[0]))
            bidxs = sorted(set(bidxs))
            i = 0
            while i < len(bidxs):
                j = i + 1
                while j < len(bidxs) and bidxs[j] == bidxs[j-1] + 1:
                    j += 1
                cluster_bars = bidxs[i:j]
                c = {"setup": setup, "class": klass, "ticker": tk, "bars": cluster_bars,
                     "leftmost": cluster_bars[0], "rightmost": cluster_bars[-1],
                     "size": len(cluster_bars), "is_winner": False, "entry_idx": None}
                all_clusters.append(c)
                ticker_to_cluster_ids[tk].append(len(all_clusters) - 1)
                i = j

        # tag winners
        for tk, ed in examples_by_setup[setup]:
            al = ticker_alignment.get(tk)
            if al is None: continue
            df_a, dates_a = al
            hits = np.where(dates_a == ed)[0]
            if len(hits) == 0: continue
            eidx = int(hits[0])
            for cid in ticker_to_cluster_ids[tk]:
                c = all_clusters[cid]
                if c["setup"] != setup: continue
                if c["leftmost"] <= eidx <= c["rightmost"] + 1:
                    c["is_winner"] = True; c["entry_idx"] = eidx; break

    # Per-firing-bar rows: (setup, class, is_winner) + N feature values + bar shape
    rows = []
    processed = 0
    for tk, cids in ticker_to_cluster_ids.items():
        if ticker_alignment.get(tk) is None: continue
        if not cids: continue
        df_a, _ = ticker_alignment[tk]
        cd, cdata = expr.get_ticker(tk)
        if cd is None or cdata is None or len(cdata) != len(df_a):
            cdata = None; gc.collect(); continue
        opens = df_a["open"].values; highs = df_a["high"].values
        lows  = df_a["low"].values;  closes = df_a["close"].values
        for cid in cids:
            c = all_clusters[cid]
            for bi in c["bars"]:
                if bi >= len(cdata): continue
                # Extract ONLY the feature columns we want
                feat_vals = cdata[bi, feature_cols_arr].astype(np.float32).copy()
                # Bar shape
                o = float(opens[bi]); h = float(highs[bi]); l = float(lows[bi]); cl = float(closes[bi])
                rng = h - l
                body_frac = (abs(cl - o) / rng) if rng > 0 else 0.0
                close_pos = ((cl - l) / rng) if rng > 0 else 0.5
                adr = float(cdata[bi, adr_col]) if adr_col is not None else np.nan
                if not np.isfinite(adr) or adr <= 0:
                    h14 = highs[max(0, bi-13):bi+1]; l14 = lows[max(0, bi-13):bi+1]
                    adr = float(np.mean(h14 - l14)) if len(h14) else np.nan
                rng_adr = (rng / adr) if (adr and adr > 0) else np.nan
                rows.append((c["setup"], c["class"], int(c["is_winner"]), rng_adr, body_frac, close_pos, feat_vals))
        cdata = None; cd = None; gc.collect()
        processed += 1
        if processed % 500 == 0: print(f"    processed {processed} tickers")
    print(f"  processed {processed} tickers  rows={len(rows)}")

    # Build arrays
    setups_arr  = np.array([r[0] for r in rows])
    classes_arr = np.array([r[1] for r in rows])
    is_w_arr    = np.array([r[2] for r in rows], dtype=bool)
    rng_arr     = np.array([r[3] for r in rows], dtype=float)
    body_arr    = np.array([r[4] for r in rows], dtype=float)
    cp_arr      = np.array([r[5] for r in rows], dtype=float)
    feat_arr    = np.stack([r[6] for r in rows], axis=0).astype(np.float32)
    del rows; gc.collect()

    # ----- Reports -----
    print()
    print("=" * 70)
    print("BAR SHAPE DISTRIBUTION + OVERLAP (per class + per setup)")
    print("=" * 70)
    shape_rows = []
    for klass in ["breakout", "fade"]:
        idx_all = (classes_arr == klass)
        w_all = is_w_arr & idx_all
        l_all = (~is_w_arr) & idx_all
        for name, arr in [("rng_adr", rng_arr), ("body_frac", body_arr), ("close_pos", cp_arr)]:
            wv = arr[w_all]; lv = arr[l_all]
            auc = auc_like(wv, lv)
            wp = winner_p50_in_loser_distribution(wv, lv)
            wp50 = np.nanmedian(wv); lp50 = np.nanmedian(lv)
            shape_rows.append({"class": klass, "setup": "ALL", "metric": name,
                                "n_w": len(wv), "n_l": len(lv),
                                "w_p50": wp50, "l_p50": lp50,
                                "w_p50_quantile_in_loser": wp, "auc": auc})
        # Per setup within class
        setups_in_class = sorted(set(setups_arr[idx_all]))
        for s in setups_in_class:
            idx_s = idx_all & (setups_arr == s)
            w_s = is_w_arr & idx_s
            l_s = (~is_w_arr) & idx_s
            for name, arr in [("rng_adr", rng_arr), ("body_frac", body_arr), ("close_pos", cp_arr)]:
                wv = arr[w_s]; lv = arr[l_s]
                auc = auc_like(wv, lv)
                wp = winner_p50_in_loser_distribution(wv, lv)
                wp50 = np.nanmedian(wv); lp50 = np.nanmedian(lv)
                shape_rows.append({"class": klass, "setup": s, "metric": name,
                                    "n_w": len(wv), "n_l": len(lv),
                                    "w_p50": wp50, "l_p50": lp50,
                                    "w_p50_quantile_in_loser": wp, "auc": auc})
    shape_df = pd.DataFrame(shape_rows)
    shape_path = os.path.join(OUT_DIR, "17_overlap_shape.csv")
    shape_df.to_csv(shape_path, index=False)
    print(f"  wrote {shape_path}")
    for klass in ["breakout", "fade"]:
        sub = shape_df[shape_df["class"] == klass]
        print(f"\n[{klass}]  bar shape:")
        print(f"  {'setup':>6} {'metric':>10} {'nW':>5} {'nL':>6} {'w_p50':>7} {'l_p50':>7} {'w50%in_L':>9} {'AUC':>6}")
        for _, r in sub.iterrows():
            print(f"  {r['setup']:>6} {r['metric']:>10} {r['n_w']:>5} {r['n_l']:>6} {r['w_p50']:>+7.3f} {r['l_p50']:>+7.3f} {r['w_p50_quantile_in_loser']:>9.3f} {r['auc']:>6.3f}")

    print()
    print("=" * 70)
    print(f"TOP FEATURES: overlap per class + per setup (AUC = fraction winner > loser)")
    print("=" * 70)
    feat_rows = []
    for klass, top_feats in [("breakout", top_br), ("fade", top_fd)]:
        idx_all = (classes_arr == klass)
        # Map feature name -> column index in our pulled feat_arr (uses feature_union order)
        fi_map = {n: feature_union.index(n) for n in top_feats}
        for feat_name in top_feats:
            col = fi_map[feat_name]
            for grp_name, idx_grp in [("ALL", idx_all)] + [(s, idx_all & (setups_arr == s))
                                                           for s in sorted(set(setups_arr[idx_all]))]:
                w_g = is_w_arr & idx_grp
                l_g = (~is_w_arr) & idx_grp
                wv = feat_arr[w_g, col]; lv = feat_arr[l_g, col]
                if (~np.isfinite(wv)).all() or (~np.isfinite(lv)).all(): continue
                auc = auc_like(wv, lv)
                wp = winner_p50_in_loser_distribution(wv, lv)
                wp50 = float(np.nanmedian(wv)); lp50 = float(np.nanmedian(lv))
                feat_rows.append({"class": klass, "setup": grp_name, "feature": feat_name,
                                   "n_w": int((w_g & np.isfinite(feat_arr[:, col])).sum()),
                                   "n_l": int((l_g & np.isfinite(feat_arr[:, col])).sum()),
                                   "w_p50": wp50, "l_p50": lp50,
                                   "w_p50_quantile_in_loser": wp, "auc": auc})
    feat_df = pd.DataFrame(feat_rows)
    feat_path = os.path.join(OUT_DIR, "17_overlap_features.csv")
    feat_df.to_csv(feat_path, index=False)
    print(f"  wrote {feat_path}")

    # Terminal summary: for each class, top features by |auc - 0.5| at ALL level
    for klass in ["breakout", "fade"]:
        all_sub = feat_df[(feat_df["class"] == klass) & (feat_df["setup"] == "ALL")].copy()
        all_sub["auc_sep"] = (all_sub["auc"] - 0.5).abs()
        all_sub = all_sub.sort_values("auc_sep", ascending=False).head(15)
        print(f"\n[{klass}] Top 15 features by AUC separation (pooled class):")
        print(f"  {'feature':<40} {'AUC':>5} {'w_p50':>9} {'l_p50':>9} {'w50%inL':>8}")
        for _, r in all_sub.iterrows():
            print(f"  {r['feature'][:40]:<40} {r['auc']:>5.3f} {r['w_p50']:>+9.3f} {r['l_p50']:>+9.3f} {r['w_p50_quantile_in_loser']:>8.3f}")

    # Per-setup best feature check: for each setup, top 5 features
    print()
    print("=" * 70)
    print("PER-SETUP TOP FEATURES (AUC at setup level)")
    print("=" * 70)
    for s in ACTIVE:
        # which class is this setup in
        klass = "fade" if s in FADE_SETUPS else "breakout"
        s_sub = feat_df[(feat_df["setup"] == s) & (feat_df["class"] == klass)].copy()
        if len(s_sub) == 0: continue
        s_sub["auc_sep"] = (s_sub["auc"] - 0.5).abs()
        s_sub = s_sub.sort_values("auc_sep", ascending=False).head(10)
        print(f"\n[{s}] (class={klass})")
        print(f"  {'feature':<40} {'AUC':>5} {'w_p50':>9} {'l_p50':>9} {'nW':>4} {'nL':>5}")
        for _, r in s_sub.iterrows():
            print(f"  {r['feature'][:40]:<40} {r['auc']:>5.3f} {r['w_p50']:>+9.3f} {r['l_p50']:>+9.3f} {r['n_w']:>4} {r['n_l']:>5}")


if __name__ == "__main__":
    main()
