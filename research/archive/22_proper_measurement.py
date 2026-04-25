"""E22: DTSS-style fade measurement + proposed breakout measurement, parallelized.

Per-class measurement logic (distinct, not mirror):

FADE (DTSS, short):
  reference = max(high) over cluster + FW_fade bars (FW=3, derived from examples)
  stop = reference
  race from ref_bar + 1 forward, bounded by earnings and MAX_FORWARD cap:
    LOSS: subsequent bar high > reference -> flat 1 ADR
    WIN:  exit signal fires -> profit = (reference - exit_close) / ADR14_at_ref
    earnings cap: forced flat -> sign of (reference - close) / ADR decides W/L/BE

BREAKOUT (HTF/BF/BASE, long):
  reference = max(high) over cluster bars only
  find breakout_bar = first forward bar within FW_brk (=5) where:
      close > reference AND close <= reference + 1 ADR (tradeable filter)
  if no such bar -> NO_TRADE / BE
  else:
    stop = reference
    race from breakout_bar + 1 forward:
      LOSS: subsequent bar low < reference -> flat 1 ADR
      WIN:  exit fires -> profit = (exit_close - reference) / ADR14_at_ref
      earnings: forced flat -> sign decides

Apply per-setup rule (from E18/E19). Rule evaluation uses the feature values
at any firing bar of the cluster. Compare rule-pass vs rule-fail.

Output: research/out/22_measurement_{setup}.csv + summary.
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

FW_FADE = 3      # derived from example data
FW_BREAKOUT = 5  # derived from example data
MAX_FORWARD_AFTER_ENTRY = 60  # race cap after entry

# Per-setup rule (LOO-validated picks from E19)
RULES = {
    "htf":  [("m_ext_slope_xavgc13_off3", ">="), ("w_ext_slope_avgc50_off2", ">=")],
    "bf":   [("w_ext_slope_xavgc100_off2", ">="), ("m_di_spread_7", ">=")],
    "base": [("m_ext_avgc5_pct", ">="), ("m_di_spread_20", ">=")],
    "dtss": [("w_es_ext50_peak_20", "<="), ("m_stoch_7", "<=")],
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


# ------- Worker -------

_W = {}


def _init_worker(universe_path, feat_cols_by_setup, exit_cond_by_setup, ex_idxs_by_key):
    sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
    os.chdir(MAIN_ROOT)
    from expr_cache_builder import ExprSeriesCache
    _W["expr"] = ExprSeriesCache()
    _W["adr_col"] = _W["expr"].expr_index("adr14")
    with open(universe_path, "rb") as f:
        _W["universe"] = pickle.load(f)
    _W["feat_cols"] = feat_cols_by_setup
    _W["exit"] = exit_cond_by_setup
    _W["ex_idxs"] = ex_idxs_by_key
    ern_map = defaultdict(list)
    import sqlite3 as _sq
    with _sq.connect(DB) as c:
        for tk, ed in c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall():
            ern_map[tk].append(str(ed)[:10])
    _W["ern"] = {tk: np.array(sorted(set(v))) for tk, v in ern_map.items()}


def _race_fade(df_a, dates_a, cdata, adr_col, cluster_bars, signal_exit_col, exit_dir, exit_thresh, ern_arr):
    # reference = max(high) over cluster + FW_fade
    fw_end = min(cluster_bars[-1] + FW_FADE, len(df_a) - 1)
    scan_start = cluster_bars[0]
    scan_end = fw_end
    highs = df_a["high"].values
    if scan_end < scan_start: return None
    scan_slice = highs[scan_start:scan_end+1]
    ref_local = int(np.argmax(scan_slice))
    ref_idx = scan_start + ref_local
    ref_price = float(scan_slice[ref_local])
    # Race from ref_idx + 1
    if ref_idx >= len(df_a) - 1: return None
    if adr_col is not None:
        adr = float(cdata[ref_idx, adr_col])
    else:
        adr = float('nan')
    if not np.isfinite(adr) or adr <= 0:
        l14 = df_a["low"].values[max(0, ref_idx-13):ref_idx+1]
        h14 = highs[max(0, ref_idx-13):ref_idx+1]
        adr = float(np.mean(h14 - l14)) if len(h14) else float('nan')
    if not np.isfinite(adr) or adr <= 0: return None

    anchor_date = dates_a[ref_idx]
    ern_bar = earnings_cap_bar(dates_a, ern_arr, anchor_date)
    race_end = min(ref_idx + MAX_FORWARD_AFTER_ENTRY, len(df_a) - 1)
    had_earnings = 0
    if ern_bar is not None and ern_bar - 1 < race_end:
        race_end = ern_bar - 1
        had_earnings = 1
    if race_end <= ref_idx: return None

    closes = df_a["close"].values
    lows = df_a["low"].values
    exit_series = cdata[:, signal_exit_col]

    for i in range(ref_idx + 1, race_end + 1):
        if highs[i] > ref_price:
            return {"outcome": "LOSS", "reason": "stop_hit", "bars_to_outcome": i - ref_idx,
                    "profit_adr": -1.0, "ref_price": ref_price, "ref_idx": ref_idx,
                    "adr": adr, "had_earnings": had_earnings, "entry_idx": ref_idx}
        v = exit_series[i]
        if not np.isnan(v):
            fired = (v >= exit_thresh) if exit_dir in (">=", "above") else (v <= exit_thresh)
            if fired:
                ec = float(closes[i])
                profit_adr = (ref_price - ec) / adr  # short
                return {"outcome": "WIN", "reason": "exit_fired", "bars_to_outcome": i - ref_idx,
                        "profit_adr": profit_adr, "ref_price": ref_price, "ref_idx": ref_idx,
                        "adr": adr, "had_earnings": had_earnings, "entry_idx": ref_idx}

    # No trigger
    if had_earnings:
        fc = float(closes[race_end])
        # short: favorable if close < ref_price
        diff = ref_price - fc
        if diff > 0:
            return {"outcome": "WIN", "reason": "earnings_flat_favorable",
                    "bars_to_outcome": race_end - ref_idx, "profit_adr": diff / adr,
                    "ref_price": ref_price, "ref_idx": ref_idx, "adr": adr, "had_earnings": 1,
                    "entry_idx": ref_idx}
        elif diff < 0:
            return {"outcome": "LOSS", "reason": "earnings_flat_adverse",
                    "bars_to_outcome": race_end - ref_idx, "profit_adr": -1.0,
                    "ref_price": ref_price, "ref_idx": ref_idx, "adr": adr, "had_earnings": 1,
                    "entry_idx": ref_idx}
        else:
            return {"outcome": "BE", "reason": "earnings_flat_zero",
                    "bars_to_outcome": race_end - ref_idx, "profit_adr": 0.0,
                    "ref_price": ref_price, "ref_idx": ref_idx, "adr": adr, "had_earnings": 1,
                    "entry_idx": ref_idx}
    return {"outcome": "BE", "reason": "held_to_end", "bars_to_outcome": race_end - ref_idx,
            "profit_adr": 0.0, "ref_price": ref_price, "ref_idx": ref_idx, "adr": adr,
            "had_earnings": 0, "entry_idx": ref_idx}


def _race_breakout(df_a, dates_a, cdata, adr_col, cluster_bars, signal_exit_col, exit_dir, exit_thresh, ern_arr):
    # reference = max(high) over cluster bars only
    highs = df_a["high"].values
    cluster_highs = [highs[bi] for bi in cluster_bars]
    ref_price = float(np.max(cluster_highs))
    rightmost = cluster_bars[-1]
    if rightmost >= len(df_a) - 1: return None
    if adr_col is not None:
        adr = float(cdata[rightmost, adr_col])
    else:
        adr = float('nan')
    if not np.isfinite(adr) or adr <= 0:
        l14 = df_a["low"].values[max(0, rightmost-13):rightmost+1]
        h14 = highs[max(0, rightmost-13):rightmost+1]
        adr = float(np.mean(h14 - l14)) if len(h14) else float('nan')
    if not np.isfinite(adr) or adr <= 0: return None

    # Find breakout_bar: first forward bar within FW_BREAKOUT where close > ref AND close <= ref + 1 ADR
    closes = df_a["close"].values
    lows = df_a["low"].values
    breakout_bar = None
    too_far = False
    for k in range(rightmost + 1, min(rightmost + 1 + FW_BREAKOUT, len(df_a))):
        ck = float(closes[k])
        if ck > ref_price:
            if ck <= ref_price + adr:
                breakout_bar = k
                break
            else:
                too_far = True
                break  # first-and-too-far: don't look further

    if breakout_bar is None:
        reason = "no_breakout_within_fw" if not too_far else "breakout_too_far"
        return {"outcome": "BE", "reason": reason, "bars_to_outcome": 0, "profit_adr": 0.0,
                "ref_price": ref_price, "ref_idx": rightmost, "adr": adr,
                "had_earnings": 0, "entry_idx": None}

    anchor_date = dates_a[breakout_bar]
    ern_bar = earnings_cap_bar(dates_a, ern_arr, anchor_date)
    race_end = min(breakout_bar + MAX_FORWARD_AFTER_ENTRY, len(df_a) - 1)
    had_earnings = 0
    if ern_bar is not None and ern_bar - 1 < race_end:
        race_end = ern_bar - 1
        had_earnings = 1
    if race_end <= breakout_bar:
        return {"outcome": "BE", "reason": "no_forward_window_after_earnings",
                "bars_to_outcome": 0, "profit_adr": 0.0,
                "ref_price": ref_price, "ref_idx": rightmost, "adr": adr,
                "had_earnings": had_earnings, "entry_idx": breakout_bar}

    exit_series = cdata[:, signal_exit_col]
    for i in range(breakout_bar + 1, race_end + 1):
        if lows[i] < ref_price:
            return {"outcome": "LOSS", "reason": "stop_hit", "bars_to_outcome": i - breakout_bar,
                    "profit_adr": -1.0, "ref_price": ref_price, "ref_idx": rightmost,
                    "adr": adr, "had_earnings": had_earnings, "entry_idx": breakout_bar}
        v = exit_series[i]
        if not np.isnan(v):
            fired = (v >= exit_thresh) if exit_dir in (">=", "above") else (v <= exit_thresh)
            if fired:
                ec = float(closes[i])
                profit_adr = (ec - ref_price) / adr
                return {"outcome": "WIN", "reason": "exit_fired", "bars_to_outcome": i - breakout_bar,
                        "profit_adr": profit_adr, "ref_price": ref_price, "ref_idx": rightmost,
                        "adr": adr, "had_earnings": had_earnings, "entry_idx": breakout_bar}

    if had_earnings:
        fc = float(closes[race_end])
        diff = fc - ref_price
        if diff > 0:
            return {"outcome": "WIN", "reason": "earnings_flat_favorable",
                    "bars_to_outcome": race_end - breakout_bar, "profit_adr": diff / adr,
                    "ref_price": ref_price, "ref_idx": rightmost, "adr": adr, "had_earnings": 1,
                    "entry_idx": breakout_bar}
        elif diff < 0:
            return {"outcome": "LOSS", "reason": "earnings_flat_adverse",
                    "bars_to_outcome": race_end - breakout_bar, "profit_adr": -1.0,
                    "ref_price": ref_price, "ref_idx": rightmost, "adr": adr, "had_earnings": 1,
                    "entry_idx": breakout_bar}
        else:
            return {"outcome": "BE", "reason": "earnings_flat_zero",
                    "bars_to_outcome": race_end - breakout_bar, "profit_adr": 0.0,
                    "ref_price": ref_price, "ref_idx": rightmost, "adr": adr, "had_earnings": 1,
                    "entry_idx": breakout_bar}
    return {"outcome": "BE", "reason": "held_to_end", "bars_to_outcome": race_end - breakout_bar,
            "profit_adr": 0.0, "ref_price": ref_price, "ref_idx": rightmost, "adr": adr,
            "had_earnings": 0, "entry_idx": breakout_bar}


def _process_ticker_batch(args):
    """args: list of (setup, ticker, [signal_dates])."""
    results = []
    expr = _W["expr"]
    universe = _W["universe"]
    adr_col = _W["adr_col"]
    feat_cols = _W["feat_cols"]
    exit_by_setup = _W["exit"]
    ex_idxs_map = _W["ex_idxs"]
    ern_map = _W["ern"]

    for setup, ticker, signal_dates in args:
        df = universe.get(ticker)
        if df is None: continue
        cd, cdata = expr.get_ticker(ticker)
        if cd is None or cdata is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None or len(cdata) != len(df_a):
            cdata = None; gc.collect(); continue

        bidxs = []
        for sd in signal_dates:
            hits = np.where(dates_a == sd)[0]
            if len(hits) == 0: continue
            bidxs.append(int(hits[0]))
        bidxs = sorted(set(bidxs))
        if not bidxs:
            cdata = None; gc.collect(); continue

        ex_idxs = ex_idxs_map.get((setup, ticker), [])
        is_fade = setup in FADE_SETUPS
        exit_info = exit_by_setup[setup]
        exit_col = exit_info["col"]
        exit_dir = exit_info["direction"]
        exit_thresh = float(exit_info["threshold"])

        # Rule features
        rule_feats = feat_cols[setup]  # list of (fname, col_idx, direction)

        # Build clusters
        i = 0
        while i < len(bidxs):
            j = i + 1
            while j < len(bidxs) and bidxs[j] == bidxs[j-1] + 1:
                j += 1
            cluster_bars = bidxs[i:j]
            is_winner = any((cluster_bars[0] <= eidx <= cluster_bars[-1] + 1) for eidx in ex_idxs)

            # Compute rule feature values (over all firing bars, orientation-aware)
            rule_vals = {}
            for fname, col_idx, direction in rule_feats:
                vals = cdata[cluster_bars, col_idx]
                if direction == ">=":
                    rule_vals[fname] = (float(np.nanmax(vals)) if np.any(np.isfinite(vals)) else np.nan, direction)
                else:
                    rule_vals[fname] = (float(np.nanmin(vals)) if np.any(np.isfinite(vals)) else np.nan, direction)

            # Race
            if is_fade:
                race_out = _race_fade(df_a, dates_a, cdata, adr_col, cluster_bars,
                                       exit_col, exit_dir, exit_thresh, ern_map.get(ticker))
            else:
                race_out = _race_breakout(df_a, dates_a, cdata, adr_col, cluster_bars,
                                           exit_col, exit_dir, exit_thresh, ern_map.get(ticker))
            if race_out is None:
                i = j; continue

            row = {
                "setup": setup, "ticker": ticker,
                "cluster_leftmost": cluster_bars[0], "cluster_rightmost": cluster_bars[-1],
                "cluster_size": len(cluster_bars),
                "is_winner": int(is_winner),
                "rule_vals": rule_vals,
                **race_out,
            }
            results.append(row)
            i = j

        cdata = None; cd = None; gc.collect()
    return results


def main():
    print("=" * 70); print("E22: DTSS-style fade + proposed breakout measurement"); print("=" * 70)

    sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
    os.chdir(MAIN_ROOT)
    from expr_cache_builder import ExprSeriesCache
    expr = ExprSeriesCache()
    all_names = expr.expr_names
    name_to_idx = {n: i for i, n in enumerate(all_names)}

    # Per-setup rule feature columns
    feat_cols_by_setup = {}
    for setup in ACTIVE:
        cols = []
        for fname, direction in RULES[setup]:
            cols.append((fname, name_to_idx[fname], direction))
        feat_cols_by_setup[setup] = cols

    # Per-setup exit condition
    exit_cond_by_setup = {}
    for setup in ACTIVE:
        with open(os.path.join(MAIN_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup}.json")) as f:
            ec = json.load(f)["top_conditions"][0]
        col = name_to_idx[ec["expression"]]
        exit_cond_by_setup[setup] = {"col": col, "direction": ec["direction"], "threshold": ec["threshold"]}
        print(f"  {setup}: exit={ec['expression']} {ec['direction']} {ec['threshold']}  rule={RULES[setup]}")

    # Build (setup, ticker, [signal_dates]) tasks
    tasks = []
    per_ticker_collected = defaultdict(dict)  # for example resolution
    for setup in ACTIVE:
        pyr_path = latest_pyramid(setup)
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        per_tk = defaultdict(list)
        for s in sigs: per_tk[s["ticker"]].append(s["date"])
        for tk, dates in per_tk.items():
            tasks.append((setup, tk, dates))
    print(f"  total tasks: {len(tasks)}")

    # Resolve example entry dates -> bar idxs per (setup, ticker) in main process
    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
    ex_dates_by_key = defaultdict(list)
    for s, t, d in ex_rows:
        ex_dates_by_key[(s, t)].append(d)
    universe_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    with open(universe_path, "rb") as f:
        universe = pickle.load(f)
    ex_idxs_by_key = {}
    for (s, t), dates in ex_dates_by_key.items():
        df = universe.get(t)
        if df is None: continue
        cd, _ = expr.get_ticker(t)
        if cd is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None: continue
        idxs = []
        for d in dates:
            hits = np.where(dates_a == d)[0]
            if len(hits) == 0: continue
            idxs.append(int(hits[0]))
        if idxs:
            ex_idxs_by_key[(s, t)] = idxs
    del universe; gc.collect()
    print(f"  example entry idxs resolved: {len(ex_idxs_by_key)} (setup,ticker) keys")

    n_workers = max(mp.cpu_count() - 2, 2)
    batch_size = max(1, len(tasks) // (n_workers * 4))
    batches = [tasks[i:i+batch_size] for i in range(0, len(tasks), batch_size)]
    print(f"  workers: {n_workers}  batches: {len(batches)}  batch_size~{batch_size}")

    all_rows = []
    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_worker,
        initargs=(universe_path, feat_cols_by_setup, exit_cond_by_setup, ex_idxs_by_key),
    ) as pool:
        futures = [pool.submit(_process_ticker_batch, b) for b in batches]
        done = 0
        for fut in as_completed(futures):
            all_rows.extend(fut.result())
            done += 1
            if done % max(1, len(batches) // 10) == 0 or done == len(batches):
                print(f"    {done}/{len(batches)} batches done, rows={len(all_rows)}")

    print(f"  collected {len(all_rows)} cluster rows")

    # Build DataFrame and compute rule thresholds (setup-specific, winner p10/p90)
    df_all = pd.DataFrame(all_rows)
    if len(df_all) == 0:
        print("  no data, abort"); return

    # Compute thresholds per setup per feature
    rule_thresh = {}
    for setup in ACTIVE:
        sub = df_all[df_all["setup"] == setup]
        winners = sub[sub["is_winner"] == 1]
        thr = {}
        for fname, direction in RULES[setup]:
            vals = []
            for _, r in winners.iterrows():
                v, _d = r["rule_vals"].get(fname, (np.nan, direction))
                if np.isfinite(v): vals.append(v)
            if vals:
                if direction == ">=":
                    t = float(np.percentile(vals, 10))
                else:
                    t = float(np.percentile(vals, 90))
                thr[fname] = (t, direction)
            else:
                thr[fname] = (np.nan, direction)
        rule_thresh[setup] = thr

    # Apply rule
    rule_pass_list = []
    for _, r in df_all.iterrows():
        setup = r["setup"]
        ok = True
        for fname, (t, direction) in rule_thresh[setup].items():
            v, _d = r["rule_vals"].get(fname, (np.nan, direction))
            if not np.isfinite(v) or not np.isfinite(t):
                ok = False; break
            if direction == ">=" and not (v >= t):
                ok = False; break
            if direction == "<=" and not (v <= t):
                ok = False; break
        rule_pass_list.append(int(ok))
    df_all["rule_pass"] = rule_pass_list

    # Report + save per setup
    print()
    print("=" * 70)
    print("RESULTS per setup")
    print("=" * 70)
    summary = []
    for setup in ACTIVE:
        sub = df_all[df_all["setup"] == setup].copy()
        if len(sub) == 0: continue
        # Save cluster-level CSV (drop rule_vals dict column)
        sub_out = sub.drop(columns=["rule_vals"])
        sub_out.to_csv(os.path.join(OUT_DIR, f"22_measurement_{setup}.csv"), index=False)

        # Split winner vs non-example, rule-pass vs fail
        thr_strs = " AND ".join(f"{fn}{dr}{t:.3f}" for fn, (t, dr) in rule_thresh[setup].items())
        print(f"\n[{setup}] rule: {thr_strs}")

        def stats(df, label):
            n = len(df)
            if n == 0: return {"label": label, "n": 0}
            n_w = int((df["outcome"] == "WIN").sum())
            n_l = int((df["outcome"] == "LOSS").sum())
            n_be = int((df["outcome"] == "BE").sum())
            wr = n_w / n; lr = n_l / n; ber = n_be / n
            winners = df[df["outcome"] == "WIN"]
            mean_w = float(winners["profit_adr"].mean()) if len(winners) else 0.0
            median_w = float(winners["profit_adr"].median()) if len(winners) else 0.0
            E = wr * mean_w - lr * 1.0
            return {"label": label, "n": n, "WIN": n_w, "BE": n_be, "LOSS": n_l,
                     "WR": wr, "BER": ber, "LR": lr, "mean_w": mean_w, "median_w": median_w, "E": E}

        winners_df = sub[sub["is_winner"] == 1]
        non_ex_df = sub[sub["is_winner"] == 0]
        rp = non_ex_df[non_ex_df["rule_pass"] == 1]
        rf = non_ex_df[non_ex_df["rule_pass"] == 0]
        grp = [stats(winners_df, "winners"), stats(rp, "rule-pass"),
               stats(rf, "rule-fail"), stats(non_ex_df, "all non-ex")]

        print(f"  {'group':<12} {'n':>5} {'W':>4} {'BE':>4} {'L':>4} {'WR%':>6} {'BE%':>6} {'LR%':>6} {'meanW':>7} {'medW':>7} {'E':>7}")
        for g in grp:
            if g["n"] == 0:
                print(f"  {g['label']:<12} (empty)"); continue
            print(f"  {g['label']:<12} {g['n']:>5} {g['WIN']:>4} {g['BE']:>4} {g['LOSS']:>4} "
                  f"{g['WR']*100:>6.1f} {g['BER']*100:>6.1f} {g['LR']*100:>6.1f} "
                  f"{g['mean_w']:>7.2f} {g['median_w']:>7.2f} {g['E']:>+7.3f}")
            summary.append({"setup": setup, **g})

        # Reason breakdown for rule-pass
        if len(rp):
            rsn = rp["reason"].value_counts().to_dict()
            print(f"  rule-pass reasons: {rsn}")

    sum_df = pd.DataFrame(summary)
    sum_path = os.path.join(OUT_DIR, "22_measurement_summary.csv")
    sum_df.to_csv(sum_path, index=False)
    print(f"\n  wrote {sum_path}")


if __name__ == "__main__":
    main()
