"""Apply per-setup rule + measure forward envelope.

For each deduped cluster (winner + non-example):
  - evaluate rule on every firing bar; cluster passes rule if ANY bar passes
  - from the cluster's rightmost firing-bar CLOSE, walk forward bounded by:
      * 60 bars (derived upper cap, looser than DTSS's max example race of ~50)
      * 1 bar before the next earnings date (from scanperfect.db)
    whichever is sooner
  - record per cluster:
      * max_fav_adr: max favorable move from rightmost_close in ADR14
          long:  (high[k] - rc) / adr14_at_rightmost, max over k
          short: (rc - low[k])  / adr14_at_rightmost, max over k
      * max_adv_adr: max adverse move
          long:  (rc - low[k])  / adr14, max over k
          short: (high[k] - rc) / adr14, max over k
      * bars_to_max_fav and bars_to_max_adv

Per setup: split rule-pass vs rule-fail, compute:
  - n per group
  - distribution of max_fav_adr and max_adv_adr
  - fraction of rule-pass where max_fav > max_adv by threshold (derived from
    example p10 favorable, NOT picked)
  - same for rule-fail

That fraction = realized WR under the envelope definition. Ratio of
rule-pass to rule-fail WR = the lift the rule provides on REAL forward action.

Output: research/out/20_envelope_{setup}.csv
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
MAX_FORWARD_BARS = 60

# Per-setup rule (from E18 top pair). Thresholds will be re-derived from
# setup-specific winner p10/p90 on current data.
RULES = {
    "htf":  [("m_bb_pctb_30", ">="), ("m_ext_slope_xavgc13_off3", ">=")],
    "bf":   [("w_ext_slope_xavgc100_off2", ">="), ("m_di_spread_7", ">=")],
    "base": [("m_ext_avgc5_pct", ">="), ("m_bb_pctb_30", ">=")],
    "dtss": [("m_ns_c_minl55_pct", ">="), ("m_stoch_7", "<=")],
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


def earnings_cap_bar(ohlcv_dates, ern_sorted, anchor_date_str):
    if ern_sorted is None or len(ern_sorted) == 0: return None
    pos = int(np.searchsorted(ern_sorted, anchor_date_str, side="right"))
    if pos >= len(ern_sorted): return None
    ern = str(ern_sorted[pos])
    bp = int(np.searchsorted(ohlcv_dates, ern, side="left"))
    return bp if bp > 0 else None


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
    print("=" * 70); print("Apply rule + measure forward envelope"); print("=" * 70)
    expr = ExprSeriesCache()
    all_names = expr.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}
    adr_col = expr.expr_index("adr14")

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  OHLCV tickers: {len(universe):,}")

    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
        ern_rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    ern_map = defaultdict(list)
    for tk, ed in ern_rows: ern_map[tk].append(str(ed)[:10])
    ern_map = {tk: np.array(sorted(set(v))) for tk, v in ern_map.items()}
    examples_by_setup = defaultdict(list)
    for s, t, d in ex_rows: examples_by_setup[s].append((t, d))

    per_setup_results = {}

    for setup in ACTIVE:
        print(f"\n---- {setup.upper()} ----")
        rule = RULES[setup]
        f1, d1 = rule[0]; f2, d2 = rule[1]
        c1 = name_to_idx[f1]; c2 = name_to_idx[f2]
        direction = "short" if setup in FADE_SETUPS else "long"

        pyr_path = latest_pyramid(setup)
        if pyr_path is None: continue
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        print(f"  raw signals: {len(sigs)}")

        # Per-ticker cluster construction
        per_ticker = defaultdict(list)
        for s in sigs: per_ticker[s["ticker"]].append(s["date"])

        # Phase 1: build clusters + collect winner firing-bar values for threshold derivation
        clusters_out = []  # per cluster row for output
        winner_f1_vals = []
        winner_f2_vals = []

        # Identify winner entry_idxs per ticker
        ex_by_ticker = defaultdict(list)
        for tk, ed in examples_by_setup[setup]:
            ex_by_ticker[tk].append(ed)

        ticker_cache = {}
        ticker_alignment = {}

        # First load alignment for all relevant tickers + collect cluster structure
        cluster_specs = []  # (ticker, cluster_bars, is_winner)
        processed = 0
        for tk, sdates in per_ticker.items():
            df = universe.get(tk)
            if df is None: continue
            cd, cdata = expr.get_ticker(tk)
            if cd is None or cdata is None:
                cdata = None; gc.collect(); continue
            df_a, dates_a = align(df, cd)
            if df_a is None or len(cdata) != len(df_a):
                cdata = None; gc.collect(); continue
            # Resolve signal bar indices
            bidxs = []
            for sd in sdates:
                hits = np.where(dates_a == sd)[0]
                if len(hits) == 0: continue
                bidxs.append(int(hits[0]))
            bidxs = sorted(set(bidxs))
            if not bidxs:
                cdata = None; gc.collect(); continue
            # Resolve example entry indices
            ex_idxs = []
            for ed in ex_by_ticker.get(tk, []):
                hits = np.where(dates_a == ed)[0]
                if len(hits) == 0: continue
                ex_idxs.append(int(hits[0]))

            # Build clusters
            i = 0
            local_clusters = []
            while i < len(bidxs):
                j = i + 1
                while j < len(bidxs) and bidxs[j] == bidxs[j-1] + 1:
                    j += 1
                bars = bidxs[i:j]
                is_winner = any((bars[0] <= eidx <= bars[-1] + 1) for eidx in ex_idxs)
                local_clusters.append((bars, is_winner))
                i = j

            # Process each cluster: feature values per firing bar, envelope from rightmost
            lows = df_a["low"].values; highs = df_a["high"].values; closes = df_a["close"].values

            for bars, is_winner in local_clusters:
                # Feature values at each firing bar (for rule evaluation)
                bar_f1 = cdata[bars, c1] if len(bars) else np.array([])
                bar_f2 = cdata[bars, c2] if len(bars) else np.array([])

                # Collect winner firing-bar feature values for threshold derivation
                if is_winner:
                    for v in bar_f1:
                        if np.isfinite(v): winner_f1_vals.append(float(v))
                    for v in bar_f2:
                        if np.isfinite(v): winner_f2_vals.append(float(v))

                # Forward envelope from rightmost firing-bar close
                rm = bars[-1]
                rc = float(closes[rm])
                if adr_col is not None:
                    adr = float(cdata[rm, adr_col])
                else:
                    adr = np.nan
                if not np.isfinite(adr) or adr <= 0:
                    h14 = highs[max(0, rm-13):rm+1]; l14 = lows[max(0, rm-13):rm+1]
                    adr = float(np.mean(h14 - l14)) if len(h14) else np.nan

                anchor_date = dates_a[rm]
                ern_bar = earnings_cap_bar(dates_a, ern_map.get(tk), anchor_date)
                race_end = min(rm + MAX_FORWARD_BARS, len(df_a) - 1)
                if ern_bar is not None and ern_bar - 1 < race_end:
                    race_end = ern_bar - 1
                if race_end <= rm or not np.isfinite(adr) or adr <= 0:
                    continue

                fw_highs = highs[rm+1:race_end+1]
                fw_lows  = lows[rm+1:race_end+1]

                if direction == "long":
                    fav_moves = (fw_highs - rc) / adr
                    adv_moves = (rc - fw_lows) / adr
                else:
                    fav_moves = (rc - fw_lows) / adr
                    adv_moves = (fw_highs - rc) / adr

                if len(fav_moves) == 0: continue
                max_fav_adr = float(fav_moves.max())
                max_adv_adr = float(adv_moves.max())
                bars_to_fav = int(fav_moves.argmax() + 1)
                bars_to_adv = int(adv_moves.argmax() + 1)

                clusters_out.append({
                    "setup": setup, "direction": direction, "ticker": tk,
                    "cluster_leftmost": bars[0], "cluster_rightmost": rm,
                    "cluster_size": len(bars),
                    "is_winner": int(is_winner),
                    "rightmost_date": str(anchor_date), "rightmost_close": rc,
                    "adr14": adr,
                    "bar_f1_max" if d1 == ">=" else "bar_f1_min":
                        float(np.nanmax(bar_f1)) if d1 == ">=" else float(np.nanmin(bar_f1)),
                    "bar_f2_max" if d2 == ">=" else "bar_f2_min":
                        float(np.nanmax(bar_f2)) if d2 == ">=" else float(np.nanmin(bar_f2)),
                    "had_earnings": int(ern_bar is not None and ern_bar - 1 <= rm + MAX_FORWARD_BARS),
                    "race_bars": race_end - rm,
                    "max_fav_adr": max_fav_adr, "max_adv_adr": max_adv_adr,
                    "bars_to_fav": bars_to_fav, "bars_to_adv": bars_to_adv,
                })
            cdata = None; cd = None; gc.collect()
            processed += 1
            if processed % 300 == 0: print(f"    processed {processed} tickers")
        print(f"  processed {processed} tickers, clusters={len(clusters_out)}")

        # Derive thresholds from winner firing-bar values
        if not winner_f1_vals or not winner_f2_vals:
            print(f"  skip — no winner feature values")
            continue
        t1 = float(np.percentile(winner_f1_vals, 10 if d1 == ">=" else 90))
        t2 = float(np.percentile(winner_f2_vals, 10 if d2 == ">=" else 90))
        print(f"  thresholds: {f1}{d1}{t1:.4f}  {f2}{d2}{t2:.4f}")

        # Evaluate rule on each cluster: cluster passes if any firing bar in that
        # cluster meets both threshold conditions. We collected bar_f1_max/min etc
        # per cluster — use them directly.
        df_out = pd.DataFrame(clusters_out)

        # For rule: the favorable direction bar_value per cluster is the extreme
        # in the favorable direction (max if d==">=", min if d=="<=")
        f1_key = "bar_f1_max" if d1 == ">=" else "bar_f1_min"
        f2_key = "bar_f2_max" if d2 == ">=" else "bar_f2_min"
        if d1 == ">=":
            ok1 = df_out[f1_key] >= t1
        else:
            ok1 = df_out[f1_key] <= t1
        if d2 == ">=":
            ok2 = df_out[f2_key] >= t2
        else:
            ok2 = df_out[f2_key] <= t2
        df_out["rule_pass"] = (ok1 & ok2).astype(int)
        df_out["t1"] = t1; df_out["t2"] = t2

        # Save
        out_path = os.path.join(OUT_DIR, f"20_envelope_{setup}.csv")
        df_out.to_csv(out_path, index=False)
        print(f"  wrote {out_path}")

        # Split + summary
        winners = df_out[df_out["is_winner"] == 1]
        non_ex  = df_out[df_out["is_winner"] == 0]
        rp = non_ex[non_ex["rule_pass"] == 1]
        rf = non_ex[non_ex["rule_pass"] == 0]

        # Envelope stats
        def pct(s, q): return float(np.percentile(s, q)) if len(s) else np.nan
        print(f"  winners (examples, n={len(winners)}):")
        print(f"    max_fav p10/p50/p90: {pct(winners.max_fav_adr,10):.2f} / {pct(winners.max_fav_adr,50):.2f} / {pct(winners.max_fav_adr,90):.2f}")
        print(f"    max_adv p10/p50/p90: {pct(winners.max_adv_adr,10):.2f} / {pct(winners.max_adv_adr,50):.2f} / {pct(winners.max_adv_adr,90):.2f}")

        print(f"  non-examples rule-PASS (n={len(rp)}):")
        print(f"    max_fav p10/p50/p90: {pct(rp.max_fav_adr,10):.2f} / {pct(rp.max_fav_adr,50):.2f} / {pct(rp.max_fav_adr,90):.2f}")
        print(f"    max_adv p10/p50/p90: {pct(rp.max_adv_adr,10):.2f} / {pct(rp.max_adv_adr,50):.2f} / {pct(rp.max_adv_adr,90):.2f}")

        print(f"  non-examples rule-FAIL (n={len(rf)}):")
        print(f"    max_fav p10/p50/p90: {pct(rf.max_fav_adr,10):.2f} / {pct(rf.max_fav_adr,50):.2f} / {pct(rf.max_fav_adr,90):.2f}")
        print(f"    max_adv p10/p50/p90: {pct(rf.max_adv_adr,10):.2f} / {pct(rf.max_adv_adr,50):.2f} / {pct(rf.max_adv_adr,90):.2f}")

        # Win definition derived from example floor
        win_threshold_fav = pct(winners.max_fav_adr, 10)
        lose_threshold_adv = 1.0  # 1 ADR per Dan's spec
        print(f"  win_threshold_fav (winner p10): {win_threshold_fav:.2f} ADR")

        def classify_envelope(row):
            if row["max_adv_adr"] >= lose_threshold_adv and row["bars_to_adv"] <= row["bars_to_fav"]:
                return "LOSS"
            if row["max_fav_adr"] >= win_threshold_fav:
                return "WIN"
            return "BE"

        winners_envelope = winners.apply(classify_envelope, axis=1)
        rp_envelope = rp.apply(classify_envelope, axis=1) if len(rp) else pd.Series([], dtype=str)
        rf_envelope = rf.apply(classify_envelope, axis=1) if len(rf) else pd.Series([], dtype=str)

        def frac(s, v): return float((s == v).mean()) if len(s) else 0.0
        print(f"  envelope outcomes (win_thresh={win_threshold_fav:.2f} ADR fav, lose if adv>=1 ADR first):")
        print(f"    winners     : W {frac(winners_envelope,'WIN'):.2f}  BE {frac(winners_envelope,'BE'):.2f}  L {frac(winners_envelope,'LOSS'):.2f}  (n={len(winners)})")
        print(f"    rule-PASS   : W {frac(rp_envelope,'WIN'):.2f}  BE {frac(rp_envelope,'BE'):.2f}  L {frac(rp_envelope,'LOSS'):.2f}  (n={len(rp)})")
        print(f"    rule-FAIL   : W {frac(rf_envelope,'WIN'):.2f}  BE {frac(rf_envelope,'BE'):.2f}  L {frac(rf_envelope,'LOSS'):.2f}  (n={len(rf)})")

        per_setup_results[setup] = {
            "t1": t1, "t2": t2, "n_winners": len(winners),
            "n_non_ex": len(non_ex), "n_rp": len(rp), "n_rf": len(rf),
            "win_thresh_fav_adr": win_threshold_fav,
            "winners_W": frac(winners_envelope, "WIN"),
            "winners_BE": frac(winners_envelope, "BE"),
            "winners_L": frac(winners_envelope, "LOSS"),
            "rp_W": frac(rp_envelope, "WIN"), "rp_BE": frac(rp_envelope, "BE"), "rp_L": frac(rp_envelope, "LOSS"),
            "rf_W": frac(rf_envelope, "WIN"), "rf_BE": frac(rf_envelope, "BE"), "rf_L": frac(rf_envelope, "LOSS"),
            "rp_mean_fav": float(rp.max_fav_adr.mean()) if len(rp) else 0,
            "rf_mean_fav": float(rf.max_fav_adr.mean()) if len(rf) else 0,
        }

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'setup':>6} {'n_rp':>5} {'n_rf':>5} {'rp_W%':>6} {'rf_W%':>6} {'WR_lift':>7} {'win_thr':>8} {'rp_mean_fav':>11}")
    for setup, r in per_setup_results.items():
        lift = r["rp_W"] - r["rf_W"]
        print(f"{setup:>6} {r['n_rp']:>5} {r['n_rf']:>5} {r['rp_W']*100:>6.1f} {r['rf_W']*100:>6.1f} {lift*100:>+7.1f} {r['win_thresh_fav_adr']:>8.2f} {r['rp_mean_fav']:>11.2f}")


if __name__ == "__main__":
    main()
