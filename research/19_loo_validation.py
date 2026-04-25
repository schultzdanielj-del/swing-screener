"""Leave-one-out validation of per-setup rules.

For each curated example in a setup:
  - Hold it out.
  - Recompute the threshold for each feature in the rule using the remaining
    N-1 winners' p10 (favorable direction) or p90 (unfavorable direction).
  - Test: does the held-out example pass the recomputed AND rule?
Aggregate across all examples → out-of-sample winner retention.
Compare to in-sample 79-84% to detect overfit.

Also: does the held-out example's rule-pass status stay stable across
different rule choices? Spot-check with top 3 rules per setup.

The loser test uses the full pool (losers don't need LOO — they weren't in
the training set).

Output: research/out/19_loo_rules.csv — per rule per setup: in-sample vs LOO.
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

# The top 2-feature rules from E18, picked per setup
RULES = {
    "htf": [
        [("m_bb_pctb_30", ">="), ("m_ext_slope_xavgc13_off3", ">=")],
        [("m_bb_pctb_30", ">="), ("w_ext_slope_avgc50_off2", ">=")],
        [("m_ext_slope_xavgc13_off3", ">="), ("w_ext_slope_avgc50_off2", ">=")],
    ],
    "bf": [
        [("w_ext_slope_xavgc100_off2", ">="), ("m_di_spread_7", ">=")],
        [("w_ext_slope_xavgc100_off2", ">="), ("m_di_spread_20", ">=")],
        [("w_ext_slope_xavgc100_off2", ">="), ("m_bb_pctb_30", ">=")],
    ],
    "base": [
        [("m_ext_avgc5_pct", ">="), ("m_bb_pctb_30", ">=")],
        [("m_bb_pctb_30", ">="), ("m_di_spread_20", ">=")],
        [("m_ext_avgc5_pct", ">="), ("m_di_spread_20", ">=")],
    ],
    "dtss": [
        [("m_ns_c_minl55_pct", ">="), ("m_stoch_7", "<=")],
        [("w_es_ext50_pullback_20", ">="), ("m_stoch_7", "<=")],
        [("w_es_ext50_peak_20", "<="), ("m_stoch_7", "<=")],
    ],
}


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


def main():
    print("=" * 70); print("LOO validation of per-setup rules"); print("=" * 70)

    # Feature union
    all_feats = set()
    for setup in ACTIVE:
        for rule in RULES[setup]:
            for f, _ in rule:
                all_feats.add(f)
    feat_list = sorted(all_feats)
    print(f"  feature union: {len(feat_list)}")

    expr = ExprSeriesCache()
    all_names = expr.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}
    feat_cols = np.array([name_to_idx[f] for f in feat_list], dtype=int)

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
    examples_by_setup = defaultdict(list)
    for s, t, d in ex_rows: examples_by_setup[s].append((t, d))

    # Build clusters + tag winners
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
                     "size": len(cluster_bars), "is_winner": False, "winner_ex_key": None}
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
                    c["is_winner"] = True
                    c["winner_ex_key"] = (tk, ed)
                    break

    # Pull firing-bar values
    # For each cluster, store: setup, is_winner, winner_ex_key, values per feature
    records = []
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
                records.append((c["setup"], int(c["is_winner"]), c["winner_ex_key"], vals, cid))
        cdata = None; cd = None; gc.collect()
        processed += 1
        if processed % 500 == 0: print(f"    processed {processed} tickers")
    print(f"  processed {processed} tickers, records={len(records)}")

    setups_arr = np.array([r[0] for r in records])
    is_w_arr   = np.array([r[1] for r in records], dtype=bool)
    ex_keys    = [r[2] for r in records]
    vals_arr   = np.stack([r[3] for r in records], axis=0).astype(np.float32)

    feat_to_col = {f: i for i, f in enumerate(feat_list)}

    report = []
    print()
    print("=" * 70)
    print("RESULTS (IS = in-sample, LOO = leave-one-out aggregate)")
    print("=" * 70)
    for setup in ACTIVE:
        print(f"\n[{setup}]")
        setup_mask = (setups_arr == setup)
        w_mask = setup_mask & is_w_arr
        l_mask = setup_mask & (~is_w_arr)
        n_w_records = int(w_mask.sum())
        n_l_records = int(l_mask.sum())
        # Distinct winner example keys (some clusters contain multiple firing bars)
        # For rule evaluation we treat EACH firing bar independently (same as E18)
        ex_keys_arr = np.array([ex_keys[i] if ex_keys[i] else ("", "") for i in range(len(ex_keys))], dtype=object)
        winner_keys = set(ex_keys[i] for i in range(len(ex_keys)) if is_w_arr[i] and ex_keys[i])
        print(f"  winner firing-bars: {n_w_records}  distinct winner examples: {len(winner_keys)}  loser firing-bars: {n_l_records}")

        for rule in RULES[setup]:
            f1, d1 = rule[0]; f2, d2 = rule[1]
            c1 = feat_to_col[f1]; c2 = feat_to_col[f2]

            # In-sample: threshold from all winners
            wv1 = vals_arr[w_mask, c1]; wv2 = vals_arr[w_mask, c2]
            wv1 = wv1[np.isfinite(wv1)]; wv2 = wv2[np.isfinite(wv2)]
            t1_is = float(np.percentile(wv1, 10 if d1 == ">=" else 90))
            t2_is = float(np.percentile(wv2, 10 if d2 == ">=" else 90))

            # IS: how many winners / losers pass
            def rule_pass(col1, t1, dir1, col2, t2, dir2, mask):
                v1 = vals_arr[mask, col1]; v2 = vals_arr[mask, col2]
                ok1 = (v1 >= t1) if dir1 == ">=" else (v1 <= t1)
                ok2 = (v2 >= t2) if dir2 == ">=" else (v2 <= t2)
                ok = ok1 & ok2 & np.isfinite(v1) & np.isfinite(v2)
                return ok

            w_pass_is = rule_pass(c1, t1_is, d1, c2, t2_is, d2, w_mask)
            l_pass_is = rule_pass(c1, t1_is, d1, c2, t2_is, d2, l_mask)
            kept_w_is = float(w_pass_is.mean())
            kept_l_is = float(l_pass_is.mean())

            # LOO on WINNER EXAMPLES (by distinct entry-example keys)
            # For each distinct winner example: remove all firing bars tied to that example,
            # recompute threshold from remaining winner firing bars' p10/p90,
            # then check whether THIS example's firing bars pass the recomputed rule.
            loo_pass_count = 0
            loo_total = 0
            for k in winner_keys:
                k_mask = np.array([ex_keys[i] == k for i in range(len(ex_keys))])
                held_mask = w_mask & k_mask
                if held_mask.sum() == 0: continue
                other_w_mask = w_mask & (~k_mask)
                wv1o = vals_arr[other_w_mask, c1]
                wv2o = vals_arr[other_w_mask, c2]
                wv1o = wv1o[np.isfinite(wv1o)]
                wv2o = wv2o[np.isfinite(wv2o)]
                if len(wv1o) < 5 or len(wv2o) < 5: continue
                t1_loo = float(np.percentile(wv1o, 10 if d1 == ">=" else 90))
                t2_loo = float(np.percentile(wv2o, 10 if d2 == ">=" else 90))
                held_pass = rule_pass(c1, t1_loo, d1, c2, t2_loo, d2, held_mask)
                # Count this example as "passing" if ANY of its firing bars pass
                if held_pass.any():
                    loo_pass_count += 1
                loo_total += 1
            loo_kept = loo_pass_count / loo_total if loo_total else 0

            # Also compute: per-example IS winner keep rate (to fair-compare with LOO)
            is_example_pass_count = 0
            for k in winner_keys:
                k_mask = np.array([ex_keys[i] == k for i in range(len(ex_keys))])
                held_mask = w_mask & k_mask
                if held_mask.sum() == 0: continue
                held_pass = rule_pass(c1, t1_is, d1, c2, t2_is, d2, held_mask)
                if held_pass.any():
                    is_example_pass_count += 1
            is_example_kept = is_example_pass_count / loo_total if loo_total else 0

            rule_name = f"{f1}{d1}{t1_is:.2f} AND {f2}{d2}{t2_is:.2f}"
            lift_is = kept_w_is - kept_l_is
            lift_loo = loo_kept - kept_l_is
            print(f"  Rule: {rule_name}")
            print(f"    IS (firing-bar): kept_W={kept_w_is:.2f}  kept_L={kept_l_is:.2f}  lift={lift_is:+.2f}")
            print(f"    IS (per-example, any-bar-passes): kept_W={is_example_kept:.2f}")
            print(f"    LOO (per-example, any-bar-passes): kept_W={loo_kept:.2f}  (delta IS-LOO={is_example_kept-loo_kept:+.2f})")
            print(f"    LOO adjusted lift (loo_W - pool_L): {lift_loo:+.2f}")

            report.append({
                "setup": setup, "rule": rule_name,
                "IS_kept_winner_fb": kept_w_is,
                "IS_kept_winner_ex": is_example_kept,
                "LOO_kept_winner_ex": loo_kept,
                "kept_loser_fb": kept_l_is,
                "IS_lift": lift_is,
                "LOO_lift": lift_loo,
                "overfit_delta": is_example_kept - loo_kept,
            })

    rep_df = pd.DataFrame(report)
    out_path = os.path.join(OUT_DIR, "19_loo_rules.csv")
    rep_df.to_csv(out_path, index=False)
    print(f"\n  wrote {out_path}")


if __name__ == "__main__":
    main()
