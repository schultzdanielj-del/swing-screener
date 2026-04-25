"""Per-setup: decorrelate top features, derive thresholds, test 2-feature combos.

From E17: top AUC features per setup. But many top breakout features are
monthly-extension variants (m_ext_avgc*, m_ext_xavgc*, m_ext_slope_*) — likely
highly correlated, so top-15 gives maybe 3-5 independent signals.

This script:
  1. For each setup, load top-30 features by AUC from E17.
  2. Pull raw values at every firing bar of winner + loser clusters.
  3. Correlation matrix across these features. Greedy decorrelation: pick the
     highest-AUC feature, then iteratively add the next feature whose abs
     correlation with all selected is below a threshold (derived, not picked).
  4. Derive a THRESHOLD for each kept feature from curated winner data:
       long-favorable feature (w_p50 > l_p50):  threshold = winner p10
       winners-below feature (w_p50 < l_p50):   threshold = winner p90
     This says "the rule catches 90% of historical winners." Example floor.
  5. Single-feature rules: fraction of winners kept + fraction of losers caught.
  6. 2-feature AND combinations of the decorrelated set: which pairs give the
     best winner-kept vs loser-filtered tradeoff?

Memory: pull only ~30 features per setup, not the full 16k. Safe.

Outputs: research/out/18_rules_{setup}.csv with single + pair results.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3, gc
from collections import defaultdict
import itertools
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
TOP_K_PER_SETUP = 30


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


def auc_mann_whitney(w, l):
    w = w[np.isfinite(w)]; l = l[np.isfinite(l)]
    if len(w) == 0 or len(l) == 0: return np.nan
    comb = np.concatenate([w, l])
    ranks = pd.Series(comb).rank(method="average").values
    wr = ranks[:len(w)]
    U = wr.sum() - len(w) * (len(w) + 1) / 2.0
    return float(U / (len(w) * len(l)))


def main():
    print("=" * 70); print("Per-setup rules: decorrelate + thresholds + pairs"); print("=" * 70)

    # Load E17 feature overlap table
    overlap = pd.read_csv(os.path.join(OUT_DIR, "17_overlap_features.csv"))

    # Pick top K per setup (at setup-level AUC, using |auc - 0.5| as rank)
    top_by_setup = {}
    for setup in ACTIVE:
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        sub = overlap[(overlap["setup"] == setup) & (overlap["class"] == klass)].copy()
        sub["sep"] = (sub["auc"] - 0.5).abs()
        sub = sub.sort_values("sep", ascending=False)
        top_by_setup[setup] = sub.head(TOP_K_PER_SETUP).reset_index(drop=True)
        print(f"  {setup}: {len(sub)} features, top {TOP_K_PER_SETUP} by AUC sep")

    # Union of all features across setups we need to pull
    feat_union = []
    for setup in ACTIVE:
        for f in top_by_setup[setup]["feature"].tolist():
            if f not in feat_union: feat_union.append(f)
    print(f"  union features: {len(feat_union)}")

    expr = ExprSeriesCache()
    all_names = expr.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}
    feat_cols = np.array([name_to_idx[n] for n in feat_union], dtype=int)
    adr_col = expr.expr_index("adr14")

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
    examples_by_setup = defaultdict(list)
    for s, t, d in ex_rows: examples_by_setup[s].append((t, d))

    # Build clusters (same pattern as E16/E17)
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

    # Pull feature values at every firing bar
    rows = []
    processed = 0
    for tk, cids in ticker_to_cluster_ids.items():
        if ticker_alignment.get(tk) is None: continue
        if not cids: continue
        cd, cdata = expr.get_ticker(tk)
        if cd is None or cdata is None: continue
        df_a, _ = ticker_alignment[tk]
        if len(cdata) != len(df_a): cdata = None; gc.collect(); continue
        for cid in cids:
            c = all_clusters[cid]
            for bi in c["bars"]:
                if bi >= len(cdata): continue
                vals = cdata[bi, feat_cols].astype(np.float32).copy()
                rows.append((c["setup"], c["class"], int(c["is_winner"]), vals))
        cdata = None; cd = None; gc.collect()
        processed += 1
        if processed % 500 == 0: print(f"    processed {processed} tickers")
    print(f"  processed {processed} tickers, rows={len(rows)}")

    setups_arr  = np.array([r[0] for r in rows])
    is_w_arr    = np.array([r[2] for r in rows], dtype=bool)
    vals_arr    = np.stack([r[3] for r in rows], axis=0).astype(np.float32)
    del rows; gc.collect()

    # For each setup: decorrelate top features greedily, derive thresholds, test pairs
    all_report_rows = []
    for setup in ACTIVE:
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        idx_setup = (setups_arr == setup)
        w_idx = idx_setup & is_w_arr
        l_idx = idx_setup & (~is_w_arr)

        # Candidate features (setup-level) with their orientation (high-winner vs low-winner)
        cand_names = top_by_setup[setup]["feature"].tolist()
        cand_cols = [feat_union.index(n) for n in cand_names]

        # Compute per-feature values for this setup's winners and losers
        W = vals_arr[w_idx][:, cand_cols]  # (nW, n_cand)
        L = vals_arr[l_idx][:, cand_cols]  # (nL, n_cand)

        # Orientation: +1 if winners higher, -1 if winners lower
        oris = []
        for j in range(W.shape[1]):
            w_vals = W[:, j]; l_vals = L[:, j]
            auc = auc_mann_whitney(w_vals, l_vals)
            oris.append(1 if auc > 0.5 else -1)
        oris = np.array(oris)

        # Signed values (so higher = better by orientation)
        W_signed = W * oris
        L_signed = L * oris

        # Correlation matrix on WINNERS for decorrelation
        # Use pandas to handle NaN-aware correlation
        W_df = pd.DataFrame(W_signed, columns=cand_names)
        corr = W_df.corr().fillna(0).values
        # Also compute per-feature AUC (using original values + orientation so AUC >= 0.5)
        aucs_signed = []
        for j in range(W.shape[1]):
            auc = auc_mann_whitney(W[:, j], L[:, j])
            if auc < 0.5: auc = 1 - auc
            aucs_signed.append(auc)
        aucs_signed = np.array(aucs_signed)

        # Greedy decorrelate: start with highest AUC, then add next whose max |corr| with any selected is < 0.7
        CORR_CAP = 0.7  # conservative; would rather keep a decorrelated set small than let redundancy in
        rank_order = np.argsort(-aucs_signed)
        selected = [rank_order[0]]
        for j in rank_order[1:]:
            max_corr = max(abs(corr[j, k]) for k in selected)
            if max_corr < CORR_CAP:
                selected.append(int(j))
            if len(selected) >= 8:
                break

        print(f"\n[{setup}] decorrelated set: {len(selected)} features (|corr| < {CORR_CAP})")
        print(f"  {'feat':<38} {'AUC':>5} {'ori':>3} {'w_p10':>9} {'w_p50':>9} {'l_p50':>9} {'l_p90':>9}")

        for j in selected:
            fname = cand_names[j]
            w_vals = W[:, j][np.isfinite(W[:, j])]
            l_vals = L[:, j][np.isfinite(L[:, j])]
            # threshold: p10 of winners in the favorable direction
            if oris[j] > 0:
                thresh = float(np.percentile(w_vals, 10))  # at least 90% of winners >= thresh
                kept_w_frac = float(np.mean(w_vals >= thresh))
                kept_l_frac = float(np.mean(l_vals >= thresh))
                direction = ">="
            else:
                thresh = float(np.percentile(w_vals, 90))
                kept_w_frac = float(np.mean(w_vals <= thresh))
                kept_l_frac = float(np.mean(l_vals <= thresh))
                direction = "<="
            auc = aucs_signed[j]
            print(f"  {fname[:38]:<38} {auc:>5.3f} {oris[j]:>+3d} "
                  f"{np.percentile(w_vals,10):>+9.3f} {np.percentile(w_vals,50):>+9.3f} "
                  f"{np.percentile(l_vals,50):>+9.3f} {np.percentile(l_vals,90):>+9.3f}")
            all_report_rows.append({"setup": setup, "class": klass, "type": "single",
                                     "features": fname, "direction": direction, "threshold": thresh,
                                     "ori": int(oris[j]), "auc": auc,
                                     "kept_winner_frac": kept_w_frac, "kept_loser_frac": kept_l_frac,
                                     "lift": kept_w_frac - kept_l_frac})

        # 2-feature AND combinations across the decorrelated set
        if len(selected) < 2:
            continue
        print(f"\n[{setup}] Top 15 2-feature AND combos (by lift = winner_kept - loser_kept):")
        pair_results = []
        for (i1, i2) in itertools.combinations(selected, 2):
            f1 = cand_names[i1]; f2 = cand_names[i2]
            o1 = oris[i1]; o2 = oris[i2]
            w1 = W[:, i1]; w2 = W[:, i2]
            l1 = L[:, i1]; l2 = L[:, i2]
            w1f = np.isfinite(w1); w2f = np.isfinite(w2)
            l1f = np.isfinite(l1); l2f = np.isfinite(l2)
            wf_mask = w1f & w2f
            lf_mask = l1f & l2f
            w1g = w1[wf_mask]; w2g = w2[wf_mask]
            l1g = l1[lf_mask]; l2g = l2[lf_mask]
            if len(w1g) < 10: continue
            t1 = float(np.percentile(w1g, 10 if o1 > 0 else 90))
            t2 = float(np.percentile(w2g, 10 if o2 > 0 else 90))
            w_ok1 = (w1g >= t1) if o1 > 0 else (w1g <= t1)
            w_ok2 = (w2g >= t2) if o2 > 0 else (w2g <= t2)
            l_ok1 = (l1g >= t1) if o1 > 0 else (l1g <= t1)
            l_ok2 = (l2g >= t2) if o2 > 0 else (l2g <= t2)
            kept_w = float(np.mean(w_ok1 & w_ok2))
            kept_l = float(np.mean(l_ok1 & l_ok2))
            lift = kept_w - kept_l
            pair_results.append({
                "f1": f1, "t1": t1, "o1": o1, "f2": f2, "t2": t2, "o2": o2,
                "kept_w": kept_w, "kept_l": kept_l, "lift": lift
            })
        pair_df = pd.DataFrame(pair_results).sort_values("lift", ascending=False)
        print(f"  {'f1':<30} {'t1':>8} {'f2':<30} {'t2':>8} {'kW':>5} {'kL':>5} {'lift':>5}")
        for _, r in pair_df.head(15).iterrows():
            dir1 = ">=" if r["o1"] > 0 else "<="
            dir2 = ">=" if r["o2"] > 0 else "<="
            print(f"  {r['f1'][:30]:<30} {dir1}{r['t1']:>7.2f} {r['f2'][:30]:<30} {dir2}{r['t2']:>7.2f} "
                  f"{r['kept_w']:>5.2f} {r['kept_l']:>5.2f} {r['lift']:>+5.2f}")
        for _, r in pair_df.iterrows():
            all_report_rows.append({"setup": setup, "class": klass, "type": "pair",
                                     "features": f"{r['f1']}&{r['f2']}",
                                     "direction": "AND",
                                     "threshold": f"{r['f1']}{'>='if r['o1']>0 else '<='}{r['t1']:.4f}, "
                                                  f"{r['f2']}{'>='if r['o2']>0 else '<='}{r['t2']:.4f}",
                                     "kept_winner_frac": r["kept_w"], "kept_loser_frac": r["kept_l"],
                                     "lift": r["lift"]})

    out_df = pd.DataFrame(all_report_rows)
    out_path = os.path.join(OUT_DIR, "18_rules_all.csv")
    out_df.to_csv(out_path, index=False)
    print(f"\n  wrote {out_path}")


if __name__ == "__main__":
    main()
