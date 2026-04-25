"""Forward-tape panel builder for WILD (non-example) clusters.

Mirror of forward_tape_panel.py. Source = the presignal ∩ pyramid intersection
pool (research/classifier_pool/{setup}_pool.json), filtered to is_example=0.

Same feature extraction, same schema (minus entry-related fields, since wild
clusters have no DB entry). Output keyed on cluster_id + ticker + signal_date.

Caveat: reads the stale intersection pool (pre pyramid regrind, pre DB cleanup
reflected in pyramid). Feature distributions are indicative; membership will
refresh after regrind but distribution shapes shouldn't change materially.
"""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

MAIN = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE = os.path.join(MAIN, "local_runner", "cache")
DB = os.path.join(MAIN, "data", "scanperfect.db")
OUT_DIR = os.path.join(WORKTREE, "research", "forward_tape_panel")
N_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
WEEKLY_STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
PRESIGNAL_SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
POOL_DIR = os.path.join(WORKTREE, "research", "classifier_pool")
SIGNAL_EXIT_DIR = os.path.join(MAIN, "data", "signal_exit_grind")

sys.path.insert(0, os.path.join(MAIN, "local_runner"))
from expr_cache_builder import load_ticker_cache  # noqa: E402

BREAKOUT_SETUPS = ["htf", "bf", "base"]
FADE_SETUPS = ["dtss", "3-4db"]
DIRECTION = {"htf": +1, "bf": +1, "base": +1, "dtss": -1, "3-4db": -1}
SETUPS_TO_RUN = FADE_SETUPS  # fades only this session; breakouts already built
MAX_FORWARD = 120


def load_universe():
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def load_earnings_map():
    with sqlite3.connect(DB) as c:
        ern = defaultdict(list)
        for tk, d in c.execute("SELECT ticker, earnings_date FROM earnings_dates"):
            ern[tk].append(str(d)[:10])
    return {t: np.array(sorted(set(v)), dtype="<U10") for t, v in ern.items()}


def load_n_bars(setup):
    with open(os.path.join(N_DIR, f"{setup}_summary.json")) as f:
        return int(json.load(f)["N_bars"])


def load_w_n(setup):
    p = os.path.join(WEEKLY_STAGE_A_DIR, f"{setup}_stage_a.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        d = json.load(f)
    return int(d.get("W_N") or d.get("w_n") or 0) or None


def load_exit_rule(setup):
    p = os.path.join(SIGNAL_EXIT_DIR, f"signal_exit_{setup}.json")
    with open(p) as f:
        d = json.load(f)
    top = None
    for key in ("top_conditions", "candidates", "conditions"):
        if key in d and isinstance(d[key], list) and d[key]:
            top = d[key][0]; break
    if top is None:
        for v in d.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "expression" in v[0]:
                top = v[0]; break
    return {"expr": top["expression"], "op": top["direction"], "thr": float(top["threshold"])}


def load_presignal_pre_dedup(setup):
    with open(os.path.join(PRESIGNAL_SCAN_DIR, f"{setup}_passes.pkl"), "rb") as f:
        d = pickle.load(f)
    return d["pre_dedup_by_ticker"]


def load_expr_vocab():
    with open(os.path.join(CACHE, "expr_series", "_manifest.json")) as f:
        m = json.load(f)
    return {n: i for i, n in enumerate(m["expr_names"])}


def load_wild_clusters(setup):
    """Return non-example clusters from the intersection pool."""
    p = os.path.join(POOL_DIR, f"{setup}_pool.json")
    with open(p) as f:
        d = json.load(f)
    return [c for c in d["clusters"] if int(c.get("is_example", 0)) == 0]


def dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def compute_adr14(df, at_idx):
    lo = max(0, at_idx - 13)
    h = df["high"].values[lo:at_idx + 1].astype(float)
    l = df["low"].values[lo:at_idx + 1].astype(float)
    return float(np.mean(h - l))


def cluster_run_backward(pre_set, sig_idx):
    run = [sig_idx]; k = sig_idx - 1
    while k in pre_set:
        run.append(k); k -= 1
    run.reverse()
    return run


def earnings_cap_offset(ern_arr, ds, sig_idx):
    df_end_off = len(ds) - 1 - sig_idx
    cap = min(MAX_FORWARD, df_end_off)
    if ern_arr is None or len(ern_arr) == 0:
        return cap
    sig_date = ds[sig_idx]
    pos = int(np.searchsorted(ern_arr, sig_date, side="right"))
    if pos >= len(ern_arr):
        return cap
    ern_date = ern_arr[pos]
    ern_bar = int(np.searchsorted(ds, ern_date, side="left"))
    if ern_bar <= sig_idx:
        return cap
    return max(0, min(cap, ern_bar - 1 - sig_idx))


def locate_expr_fire(cache_dates, cache_data, col, op, thr, sig_cache_idx, max_off):
    end = min(sig_cache_idx + max_off, len(cache_dates) - 1)
    for k in range(sig_cache_idx + 1, end + 1):
        v = float(cache_data[k, col])
        if np.isnan(v):
            continue
        hit = (op == "<=" and v <= thr) or (op == ">=" and v >= thr) \
              or (op == "<" and v < thr) or (op == ">" and v > thr)
        if hit:
            return k - sig_cache_idx
    return None


def process_cluster(cluster_id, ticker, sig_idx, setup, universe, ern_map,
                    expr_col, exit_rule, n_bars, v3_window, pre_dedup, direction):
    include_pivots = (direction == +1)
    df = universe.get(ticker)
    if df is None:
        return None, [], "no_df"
    ds = dates_str(df)
    if sig_idx < 0 or sig_idx >= len(ds):
        return None, [], f"sig_idx_oor={sig_idx}"
    sig_date = ds[sig_idx]
    sig_open = float(df["open"].values[sig_idx])
    sig_high = float(df["high"].values[sig_idx])
    sig_low = float(df["low"].values[sig_idx])
    sig_close = float(df["close"].values[sig_idx])
    adr = compute_adr14(df, sig_idx)
    if adr <= 0 or np.isnan(adr):
        return None, [], "adr<=0"

    if include_pivots:
        v1_lo = max(0, sig_idx - n_bars + 1)
        v1 = float(np.max(df["high"].values[v1_lo:sig_idx + 1]))
        pre_set = set(pre_dedup.get(ticker, []))
        if sig_idx in pre_set:
            run = cluster_run_backward(pre_set, sig_idx)
            v2 = float(np.max(df["high"].values[run[0]:run[-1] + 1]))
        else:
            v2 = sig_high
        v3_lo = max(0, sig_idx - v3_window + 1)
        v3 = float(np.max(df["high"].values[v3_lo:sig_idx + 1]))
        v4 = float(np.max(df["close"].values[v1_lo:sig_idx + 1]))
        if sig_idx - 1 >= v1_lo:
            v5 = float(np.max(df["high"].values[v1_lo:sig_idx]))
        else:
            v5 = float("nan")

    earn_cap_off = earnings_cap_offset(ern_map.get(ticker), ds, sig_idx)

    cache_dates, cache_data = load_ticker_cache(ticker, cast_to_float32=False)
    if cache_dates is None:
        return None, [], "no_expr_cache"
    c_ds = np.array([str(x)[:10] for x in cache_dates])
    cm = np.where(c_ds == sig_date)[0]
    if len(cm) == 0:
        return None, [], "sig_date_not_in_expr_cache"
    sig_cache_idx = int(cm[0])

    fire_off = locate_expr_fire(
        cache_dates, cache_data, expr_col, exit_rule["op"], exit_rule["thr"],
        sig_cache_idx, earn_cap_off,
    )

    scalar = {
        "setup": setup, "cluster_id": cluster_id, "ticker": ticker,
        "signal_date": sig_date, "signal_bar_idx": int(sig_idx),
        "sig_open": sig_open, "sig_high": sig_high, "sig_low": sig_low, "sig_close": sig_close,
        "adr_at_signal": adr,
        "direction": int(direction),
        "earnings_cap_offset": int(earn_cap_off),
        "exit_fire_offset": int(fire_off) if fire_off is not None else None,
    }
    if include_pivots:
        scalar.update({"v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5})

    ts_rows = []
    cum_max_high = -np.inf; cum_min_low = np.inf
    exit_hit = False
    exit_fire_bar = sig_idx + fire_off if fire_off is not None else None
    if include_pivots:
        sighigh_hit = False; siglow_hit = False
        v_hits = [False] * 5
        levels = [v1, v2, v3, v4, v5]

    for off in range(1, earn_cap_off + 1):
        bar = sig_idx + off
        if bar >= len(df):
            break
        o = float(df["open"].values[bar])
        h = float(df["high"].values[bar])
        l = float(df["low"].values[bar])
        c = float(df["close"].values[bar])
        v = float(df["volume"].values[bar]) if "volume" in df.columns else float("nan")
        cum_max_high = max(cum_max_high, h)
        cum_min_low = min(cum_min_low, l)
        body = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        rng = h - l

        row_exit = (exit_fire_bar == bar)
        if row_exit: exit_hit = True

        row = {
            "setup": setup, "cluster_id": cluster_id, "ticker": ticker,
            "offset": off, "date": ds[bar],
            "o": o, "h": h, "l": l, "c": c, "v": v,
            "close_rel": c / sig_close,
            "high_rel": h / sig_close,
            "low_rel": l / sig_close,
            "body_adr": body / adr,
            "upper_wick_adr": upper_wick / adr,
            "lower_wick_adr": lower_wick / adr,
            "range_adr": rng / adr,
            "cum_max_high": cum_max_high,
            "cum_min_low": cum_min_low,
            "exit_fire": row_exit, "exit_fired_by_here": exit_hit,
            "past_earnings_cap": off == earn_cap_off,
        }
        if include_pivots:
            row_sighigh = (not sighigh_hit) and (h > sig_high)
            if row_sighigh: sighigh_hit = True
            row_siglow = (not siglow_hit) and (l < sig_low)
            if row_siglow: siglow_hit = True
            row_vs = []
            for i, lv in enumerate(levels):
                fire = False
                if (not v_hits[i]) and (not np.isnan(lv)) and (h > lv):
                    v_hits[i] = True; fire = True
                row_vs.append(fire)
            row.update({
                "run_up_adr": (cum_max_high - sig_close) / adr,
                "drawdown_adr": (cum_min_low - sig_close) / adr,
                "sighigh_breach": row_sighigh, "sighigh_breached_by_here": sighigh_hit,
                "siglow_breach": row_siglow, "siglow_breached_by_here": siglow_hit,
                "v1_breach": row_vs[0], "v1_breached_by_here": v_hits[0],
                "v2_breach": row_vs[1], "v2_breached_by_here": v_hits[1],
                "v3_breach": row_vs[2], "v3_breached_by_here": v_hits[2],
                "v4_breach": row_vs[3], "v4_breached_by_here": v_hits[3],
                "v5_breach": row_vs[4], "v5_breached_by_here": v_hits[4],
            })
        ts_rows.append(row)

    return scalar, ts_rows, None


def build_wild_panel_for_setup(setup, universe, ern_map, expr_name2idx):
    direction = DIRECTION[setup]
    n_bars = load_n_bars(setup)
    w_n = load_w_n(setup)
    v3_window = (w_n * 5) if w_n else n_bars
    exit_rule = load_exit_rule(setup)
    pre_dedup = load_presignal_pre_dedup(setup)
    clusters = load_wild_clusters(setup)

    print(f"\n=== {setup} === direction={direction:+d}  wild_clusters={len(clusters)}  N_bars={n_bars}  "
          f"V3_window={v3_window}  exit={exit_rule['expr']} {exit_rule['op']} {exit_rule['thr']}",
          flush=True)

    expr_col = expr_name2idx.get(exit_rule["expr"])
    if expr_col is None:
        raise RuntimeError(f"{setup}: exit expr {exit_rule['expr']} not in cache vocab")

    scalars_rows = []; ts_rows = []; unresolvable = []
    for c in clusters:
        cid = int(c["cluster_id"]); tk = c["ticker"]
        sig_idx = int(c["rightmost"]["bar_idx"])
        scalar, rows, err = process_cluster(
            cid, tk, sig_idx, setup, universe, ern_map,
            expr_col, exit_rule, n_bars, v3_window, pre_dedup, direction,
        )
        if err:
            unresolvable.append((tk, sig_idx, err)); continue
        scalars_rows.append(scalar); ts_rows.extend(rows)

    return pd.DataFrame(scalars_rows), pd.DataFrame(ts_rows), unresolvable


def main():
    assert OUT_DIR.startswith(WORKTREE)
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Loading universe OHLCV...", flush=True)
    universe = load_universe()
    print(f"  tickers: {len(universe):,}")
    assert len(universe) > 11000

    print("Loading earnings...", flush=True)
    ern_map = load_earnings_map()

    print("Loading expression vocab...", flush=True)
    expr_name2idx = load_expr_vocab()

    for setup in SETUPS_TO_RUN:
        scalars, ts, unresolvable = build_wild_panel_for_setup(
            setup, universe, ern_map, expr_name2idx)
        if unresolvable:
            print(f"  !! {setup} unresolvable: {len(unresolvable)}  examples: {unresolvable[:3]}")
        s_path = os.path.join(OUT_DIR, f"{setup}_wild_scalars.pkl")
        ts_path = os.path.join(OUT_DIR, f"{setup}_wild_timeseries.pkl")
        assert s_path.startswith(WORKTREE) and ts_path.startswith(WORKTREE)
        scalars.to_pickle(s_path); ts.to_pickle(ts_path)
        print(f"  wrote {len(scalars)} scalar rows + {len(ts):,} ts rows")


if __name__ == "__main__":
    main()
