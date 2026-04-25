"""E21: per-rule expectancy + alt DTSS rule + parallel workers.

Expectancy (per rule-pass group per setup):
  E = WR_envelope * mean_winner_fav_adr - LR_envelope * 1.0

Where:
  WIN_envelope   = max_fav >= winner_p10_fav AND adv stays below 1 ADR
                   until that fav level is reached
  LOSS_envelope  = max_adv reaches 1 ADR before max_fav reaches winner_p10_fav
  BE             = neither (scratch)

mean_winner_fav_adr = mean max_fav across envelope-WIN signals in that group.

Rules tested per setup:
  - primary (from E18)
  - top alternative (from LOO output) for DTSS

Parallel: ProcessPoolExecutor across ticker batches. Each worker loads its
own expr cache handle and processes its ticker subset.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3, gc
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

FADE_SETUPS = {"dtss", "3-4db"}
ACTIVE = ["htf", "bf", "base", "dtss"]
MAX_FORWARD_BARS = 60

RULES = [
    ("htf",  "primary",  [("m_bb_pctb_30", ">="), ("m_ext_slope_xavgc13_off3", ">=")]),
    ("htf",  "alt",      [("m_ext_slope_xavgc13_off3", ">="), ("w_ext_slope_avgc50_off2", ">=")]),
    ("bf",   "primary",  [("w_ext_slope_xavgc100_off2", ">="), ("m_di_spread_7", ">=")]),
    ("base", "primary",  [("m_ext_avgc5_pct", ">="), ("m_bb_pctb_30", ">=")]),
    ("base", "alt",      [("m_ext_avgc5_pct", ">="), ("m_di_spread_20", ">=")]),
    ("dtss", "primary",  [("m_ns_c_minl55_pct", ">="), ("m_stoch_7", "<=")]),
    ("dtss", "alt",      [("w_es_ext50_peak_20", "<="), ("m_stoch_7", "<=")]),
    ("dtss", "alt2",     [("w_es_ext50_pullback_20", ">="), ("m_stoch_7", "<=")]),
]


def ohlcv_dates_str(df):
    import pandas as _pd
    if _pd.api.types.is_datetime64_any_dtype(df["date"]):
        return _pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
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


# ---- Worker ----

_W_expr = None
_W_universe = None
_W_ern_map = None
_W_feat_cols_by_setup = None
_W_ex_idxs_by_setup = None
_W_adr_col = None


def _init_worker(universe_path, feat_cols_by_setup, ex_idxs_by_setup):
    global _W_expr, _W_universe, _W_ern_map, _W_feat_cols_by_setup, _W_ex_idxs_by_setup, _W_adr_col
    sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
    os.chdir(MAIN_ROOT)
    from expr_cache_builder import ExprSeriesCache  # noqa: E402
    with open(universe_path, "rb") as f:
        _W_universe = pickle.load(f)
    _W_expr = ExprSeriesCache()
    _W_adr_col = _W_expr.expr_index("adr14")
    _W_feat_cols_by_setup = feat_cols_by_setup
    _W_ex_idxs_by_setup = ex_idxs_by_setup
    # Load earnings map locally (fresh DB connection per worker)
    import sqlite3 as _sq
    ern_map = defaultdict(list)
    with _sq.connect(DB) as c:
        for tk, ed in c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall():
            ern_map[tk].append(str(ed)[:10])
    _W_ern_map = {tk: np.array(sorted(set(v))) for tk, v in ern_map.items()}


def _process_ticker_batch(args):
    """args: list of (setup, ticker, signal_dates). Returns list of cluster result dicts."""
    out = []
    for setup, ticker, signal_dates in args:
        df = _W_universe.get(ticker)
        if df is None: continue
        cd, cdata = _W_expr.get_ticker(ticker)
        if cd is None or cdata is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None or len(cdata) != len(df_a):
            cdata = None; gc.collect(); continue

        # Bar indices
        bidxs = []
        for sd in signal_dates:
            hits = np.where(dates_a == sd)[0]
            if len(hits) == 0: continue
            bidxs.append(int(hits[0]))
        bidxs = sorted(set(bidxs))
        if not bidxs:
            cdata = None; gc.collect(); continue

        # Example entry indices for this ticker+setup
        ex_idxs_key = (setup, ticker)
        ex_idxs = _W_ex_idxs_by_setup.get(ex_idxs_key, [])

        lows = df_a["low"].values; highs = df_a["high"].values; closes = df_a["close"].values
        direction = "short" if setup in FADE_SETUPS else "long"

        # Build clusters
        i = 0
        while i < len(bidxs):
            j = i + 1
            while j < len(bidxs) and bidxs[j] == bidxs[j-1] + 1:
                j += 1
            bars = bidxs[i:j]
            is_winner = any((bars[0] <= eidx <= bars[-1] + 1) for eidx in ex_idxs)
            rm = bars[-1]

            # Extract feature values for each rule across all firing bars of this cluster
            feat_cols_list = _W_feat_cols_by_setup[setup]  # list of (feat_name, col_idx, direction)
            feat_vals = {}
            for f_name, col_idx, _dir in feat_cols_list:
                vals = cdata[bars, col_idx]
                # orientation-ready extreme: max if ">=" else min
                if _dir == ">=":
                    feat_vals[f_name] = float(np.nanmax(vals)) if np.any(np.isfinite(vals)) else np.nan
                else:
                    feat_vals[f_name] = float(np.nanmin(vals)) if np.any(np.isfinite(vals)) else np.nan

            # Also store first-bar feature for winner threshold derivation (use ALL firing bars of winners)
            winner_feat_vals = None
            if is_winner:
                winner_feat_vals = {}
                for f_name, col_idx, _dir in feat_cols_list:
                    vals = [float(cdata[bi, col_idx]) for bi in bars if np.isfinite(cdata[bi, col_idx])]
                    winner_feat_vals[f_name] = vals

            # Forward envelope from rightmost firing-bar close
            rc = float(closes[rm])
            if _W_adr_col is not None:
                adr = float(cdata[rm, _W_adr_col])
            else:
                adr = np.nan
            if not np.isfinite(adr) or adr <= 0:
                h14 = highs[max(0, rm-13):rm+1]; l14 = lows[max(0, rm-13):rm+1]
                adr = float(np.mean(h14 - l14)) if len(h14) else np.nan

            anchor_date = dates_a[rm]
            ern_bar = earnings_cap_bar(dates_a, _W_ern_map.get(ticker), anchor_date)
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
            max_fav = float(fav_moves.max()); max_adv = float(adv_moves.max())
            bars_to_fav = int(fav_moves.argmax() + 1)
            bars_to_adv = int(adv_moves.argmax() + 1)

            out.append({
                "setup": setup, "direction": direction, "ticker": ticker,
                "leftmost": bars[0], "rightmost": rm, "size": len(bars),
                "is_winner": int(is_winner),
                "rightmost_date": str(anchor_date), "rightmost_close": rc,
                "adr14": adr, "had_earnings": int(ern_bar is not None and ern_bar - 1 <= rm + MAX_FORWARD_BARS),
                "race_bars": race_end - rm,
                "max_fav_adr": max_fav, "max_adv_adr": max_adv,
                "bars_to_fav": bars_to_fav, "bars_to_adv": bars_to_adv,
                "feat_vals": feat_vals,
                "winner_feat_vals": winner_feat_vals,
            })
            i = j
        cdata = None; cd = None; gc.collect()
    return out


def main():
    print("=" * 70); print("E21: per-rule expectancy + alt rules + parallel workers"); print("=" * 70)

    sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
    os.chdir(MAIN_ROOT)
    from expr_cache_builder import ExprSeriesCache  # noqa
    expr_main = ExprSeriesCache()
    all_names = expr_main.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}

    # Per-setup feature columns (union of primary + alt rules)
    feat_cols_by_setup = defaultdict(list)
    for setup, label, rule in RULES:
        for fname, direction in rule:
            if (fname, name_to_idx[fname], direction) not in feat_cols_by_setup[setup]:
                feat_cols_by_setup[setup].append((fname, name_to_idx[fname], direction))
    # Dedup preserving order
    for s in list(feat_cols_by_setup):
        seen = set(); uniq = []
        for item in feat_cols_by_setup[s]:
            key = (item[0], item[2])
            if key not in seen:
                seen.add(key); uniq.append(item)
        feat_cols_by_setup[s] = uniq
    for s, fc in feat_cols_by_setup.items():
        print(f"  {s}: pulling {len(fc)} feature columns")

    # Examples → per (setup, ticker) list of entry_idxs
    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
    # Need to resolve entry_date to bar_idx per ticker, but that happens in worker after align.
    # Pass entry_DATES (strings) instead; worker resolves.
    ex_dates_by_key = defaultdict(list)
    for s, t, d in ex_rows:
        ex_dates_by_key[(s, t)].append(d)

    # Build per-setup signal lists (setup, ticker, [dates])
    tasks = []  # list of (setup, ticker, [signal_dates])
    for setup in ACTIVE:
        pyr_path = latest_pyramid(setup)
        if pyr_path is None: continue
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        per_tk = defaultdict(list)
        for s in sigs: per_tk[s["ticker"]].append(s["date"])
        for tk, dates in per_tk.items():
            tasks.append((setup, tk, dates))
    print(f"  total (setup, ticker) tasks: {len(tasks)}")

    # Resolve example dates to bar idxs per (setup, ticker) in MAIN process (one-time align)
    # This avoids re-aligning in every worker.
    universe_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(universe_path, "rb") as f:
        universe = pickle.load(f)
    ex_idxs_by_setup = {}
    for (s, t), dates in ex_dates_by_key.items():
        df = universe.get(t)
        if df is None: continue
        cd, _ = expr_main.get_ticker(t)
        if cd is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None: continue
        idxs = []
        for d in dates:
            hits = np.where(dates_a == d)[0]
            if len(hits) == 0: continue
            idxs.append(int(hits[0]))
        if idxs:
            ex_idxs_by_setup[(s, t)] = idxs
    del universe; gc.collect()

    # Batch tasks for workers
    n_workers = max(mp.cpu_count() - 2, 2)
    batch_size = max(1, len(tasks) // (n_workers * 4))
    batches = [tasks[i:i+batch_size] for i in range(0, len(tasks), batch_size)]
    print(f"  workers: {n_workers}  batches: {len(batches)}  batch_size~{batch_size}")

    all_rows = []
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_worker,
        initargs=(universe_path, dict(feat_cols_by_setup), dict(ex_idxs_by_setup)),
    ) as pool:
        futures = [pool.submit(_process_ticker_batch, b) for b in batches]
        done = 0
        for fut in as_completed(futures):
            rows = fut.result()
            all_rows.extend(rows)
            done += 1
            if done % max(1, len(batches)//10) == 0 or done == len(batches):
                print(f"    {done}/{len(batches)} batches done, rows={len(all_rows)}")

    print(f"  collected {len(all_rows)} clusters")

    # Organize per setup
    per_setup_clusters = defaultdict(list)
    for r in all_rows:
        per_setup_clusters[r["setup"]].append(r)

    # Collect winner feature value lists per setup per feature for threshold derivation
    per_setup_winner_vals = defaultdict(lambda: defaultdict(list))
    for r in all_rows:
        if r["is_winner"] and r["winner_feat_vals"]:
            for fname, vals in r["winner_feat_vals"].items():
                per_setup_winner_vals[r["setup"]][fname].extend(vals)

    # Compute thresholds per (setup, rule) — independent of rule label
    thresholds_by_rule = {}
    for setup, label, rule in RULES:
        thr = {}
        for fname, direction in rule:
            vals = per_setup_winner_vals[setup][fname]
            if not vals:
                thr[fname] = (np.nan, direction); continue
            if direction == ">=":
                t = float(np.percentile(vals, 10))
            else:
                t = float(np.percentile(vals, 90))
            thr[fname] = (t, direction)
        thresholds_by_rule[(setup, label)] = thr

    # Evaluate rules + compute per-setup expectancy
    summary = []
    for setup in ACTIVE:
        clusters = per_setup_clusters[setup]
        if not clusters: continue
        winners = [c for c in clusters if c["is_winner"]]
        non_ex  = [c for c in clusters if not c["is_winner"]]
        if not winners: continue

        # Winner p10 of max_fav_adr = win_thresh
        winner_fav = np.array([c["max_fav_adr"] for c in winners if np.isfinite(c["max_fav_adr"])])
        if len(winner_fav) == 0: continue
        win_thresh = float(np.percentile(winner_fav, 10))

        print(f"\n---- {setup.upper()} ----")
        print(f"  winners: {len(winners)}  non-examples: {len(non_ex)}")
        print(f"  win_threshold_fav (winner p10 max_fav_adr): {win_thresh:.2f} ADR")

        def envelope_class(c):
            fav = c["max_fav_adr"]; adv = c["max_adv_adr"]
            b_fav = c["bars_to_fav"]; b_adv = c["bars_to_adv"]
            # LOSS if adv reaches 1 ADR before fav reaches win_thresh
            if adv >= 1.0 and b_adv <= b_fav:
                return "LOSS"
            if fav >= win_thresh:
                return "WIN"
            return "BE"

        for label_filter in [l for (s, l, _) in RULES if s == setup]:
            for (s, l, rule) in RULES:
                if s != setup or l != label_filter: continue
                thr = thresholds_by_rule[(setup, label_filter)]
                pass_mask_non_ex = []
                for c in non_ex:
                    ok = True
                    for fname, (t, dr) in thr.items():
                        v = c["feat_vals"].get(fname, np.nan)
                        if not np.isfinite(v) or np.isnan(t):
                            ok = False; break
                        if dr == ">=" and not (v >= t):
                            ok = False; break
                        if dr == "<=" and not (v <= t):
                            ok = False; break
                    pass_mask_non_ex.append(ok)
                pass_mask_non_ex = np.array(pass_mask_non_ex)
                rp = [c for c, ok in zip(non_ex, pass_mask_non_ex) if ok]
                rf = [c for c, ok in zip(non_ex, pass_mask_non_ex) if not ok]

                def group_stats(group):
                    n = len(group)
                    if n == 0: return dict(n=0, W=0, BE=0, L=0, mean_w=0.0, E=0.0)
                    outcomes = [envelope_class(c) for c in group]
                    n_w = outcomes.count("WIN"); n_be = outcomes.count("BE"); n_l = outcomes.count("LOSS")
                    wr = n_w / n; lr = n_l / n
                    winner_favs = [c["max_fav_adr"] for c, o in zip(group, outcomes) if o == "WIN"]
                    mean_w = float(np.mean(winner_favs)) if winner_favs else 0.0
                    E = wr * mean_w - lr * 1.0
                    return dict(n=n, W=n_w, BE=n_be, L=n_l, wr=wr, lr=lr, mean_w=mean_w, E=E)

                rp_s = group_stats(rp); rf_s = group_stats(rf)
                all_s = group_stats(non_ex)

                thr_str = " AND ".join(
                    f"{fn}{dr}{t:.3f}" for fn, (t, dr) in thr.items()
                )
                print(f"  [{label_filter}] {thr_str}")
                print(f"    rule-PASS  n={rp_s['n']}  WR={rp_s['wr']*100:5.1f}%  LR={rp_s['lr']*100:5.1f}%  "
                      f"meanW={rp_s['mean_w']:.2f}  E={rp_s['E']:+.3f}")
                print(f"    rule-FAIL  n={rf_s['n']}  WR={rf_s['wr']*100:5.1f}%  LR={rf_s['lr']*100:5.1f}%  "
                      f"meanW={rf_s['mean_w']:.2f}  E={rf_s['E']:+.3f}")
                print(f"    ALL       n={all_s['n']}  WR={all_s['wr']*100:5.1f}%  LR={all_s['lr']*100:5.1f}%  "
                      f"meanW={all_s['mean_w']:.2f}  E={all_s['E']:+.3f}")

                summary.append({
                    "setup": setup, "rule": label_filter, "thresholds": thr_str,
                    "win_thresh_fav": win_thresh,
                    "n_rp": rp_s["n"], "rp_wr": rp_s["wr"], "rp_lr": rp_s["lr"], "rp_mean_w": rp_s["mean_w"], "rp_E": rp_s["E"],
                    "n_rf": rf_s["n"], "rf_wr": rf_s["wr"], "rf_lr": rf_s["lr"], "rf_mean_w": rf_s["mean_w"], "rf_E": rf_s["E"],
                    "n_all": all_s["n"], "all_wr": all_s["wr"], "all_E": all_s["E"],
                })
                break  # avoid duplicate loop

    sum_df = pd.DataFrame(summary).drop_duplicates(["setup", "rule"])
    sum_path = os.path.join(OUT_DIR, "21_expectancy_per_rule.csv")
    sum_df.to_csv(sum_path, index=False)
    print(f"\n  wrote {sum_path}")

    print()
    print("=" * 70)
    print("SUMMARY — per-setup per-rule expectancy (rule-pass group)")
    print("=" * 70)
    print(f"{'setup':>6} {'rule':>8} {'n_rp':>5} {'WR%':>6} {'LR%':>6} {'meanW':>7} {'E':>7}")
    for _, r in sum_df.iterrows():
        print(f"{r['setup']:>6} {r['rule']:>8} {int(r['n_rp']):>5} "
              f"{r['rp_wr']*100:>6.1f} {r['rp_lr']*100:>6.1f} {r['rp_mean_w']:>7.2f} {r['rp_E']:>+7.3f}")


if __name__ == "__main__":
    main()
