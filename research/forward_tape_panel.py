"""Forward-tape panel builder — breakout class (HTF, BF, BASE).

Goal: gather data about similarities of example forward tapes. No rules, no
interpretation — just extract signal-bar-anchored and forward-bar-relative
features for each of the 113 breakout examples, from sig_idx+1 through
min(earnings-1, sig_idx+MAX_FORWARD).

Constraints honored:
  - No entry-bar reference. No feature uses entry_idx directly. entry_idx is
    stored in the scalars only as metadata so we can sanity-check later.
  - Forward-looking OK (historical data).
  - Cutoff = earnings-1 relative to signal_date, or sig + MAX_FORWARD, or
    end of OHLCV, whichever comes first.

Feature set per forward bar k (k = 1..min(earnings-1-sig, 120, len(df)-1-sig)):
  date, offset, o/h/l/c/v (raw),
  close_rel, high_rel, low_rel (all close/sig_close ratios),
  body_adr, upper_wick_adr, lower_wick_adr, range_adr  (bar shape in ADR units),
  cum_max_high, cum_min_low (rolling since sig+1),
  run_up_adr    = (cum_max_high - sig_close) / adr_at_signal,
  drawdown_adr  = (cum_min_low  - sig_close) / adr_at_signal,
  sighigh_breach, sighigh_breached_by_here,
  siglow_breach,  siglow_breached_by_here,
  v1_breach .. v5_breach,
  v1_breached_by_here .. v5_breached_by_here,
  exit_fire, exit_fired_by_here,
  past_earnings_cap

Scalars per example (metadata + global quantities):
  setup, ticker, entry_date, signal_date, signal_bar_idx, entry_bar_idx,
  sig_open, sig_high, sig_low, sig_close, adr_at_signal,
  v1, v2, v3, v4, v5 (key-level values),
  earnings_cap_offset, exit_fire_offset (or None).

Key-level candidates (data-derived lookback; no hand-picked numbers):
  V1: max(high) over [sig_idx - N_bars + 1, sig_idx]  (N_bars from presignal n_derivation_cache)
  V2: max(high) over pre-dedup cluster run ending at sig_idx (presignal cluster semantics)
  V3: max(high) over (W_N * 5) daily bars (weekly-window as daily approx)
  V4: max(close) over N_bars
  V5: max(high) over [sig_idx - N_bars + 1, sig_idx - 1]  (exclude signal bar's own high)

Output:
  research/forward_tape_panel/{setup}_scalars.pkl    -- per-example scalars
  research/forward_tape_panel/{setup}_timeseries.pkl -- per-example × offset rows
  research/forward_tape_panel/summary.json           -- counts + first-look distributions
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd

MAIN = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
CACHE = os.path.join(MAIN, "local_runner", "cache")
DB = os.path.join(MAIN, "data", "scanperfect.db")
OUT_DIR = os.path.join(WORKTREE, "research", "forward_tape_panel")
N_DIR = os.path.join(WORKTREE, "research", "n_derivation_cache")
WEEKLY_STAGE_A_DIR = os.path.join(WORKTREE, "research", "presignal_weekly_stage_a")
PRESIGNAL_SCAN_DIR = os.path.join(WORKTREE, "research", "presignal_sma_band_scan")
SIGNAL_EXIT_DIR = os.path.join(MAIN, "data", "signal_exit_grind")

sys.path.insert(0, os.path.join(MAIN, "local_runner"))
from expr_cache_builder import load_ticker_cache  # noqa: E402

BREAKOUT_SETUPS = ["htf", "bf", "base"]
FADE_SETUPS = ["dtss", "3-4db"]
DIRECTION = {"htf": +1, "bf": +1, "base": +1, "dtss": -1, "3-4db": -1}
SETUPS_TO_RUN = FADE_SETUPS  # fades only this session; breakouts already built
MAX_FORWARD = 120


# ─────────────────────── data loaders ───────────────────────

def load_universe():
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def load_earnings_map():
    with sqlite3.connect(DB) as c:
        ern = defaultdict(list)
        for tk, d in c.execute("SELECT ticker, earnings_date FROM earnings_dates"):
            ern[tk].append(str(d)[:10])
    return {t: np.array(sorted(set(v)), dtype="<U10") for t, v in ern.items()}


def load_db_examples(setup):
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=? ORDER BY ticker, entry_date",
            (setup,),
        ).fetchall()
    return [{"ticker": t, "entry_date": str(d)[:10]} for t, d in rows]


def load_n_bars(setup):
    p = os.path.join(N_DIR, f"{setup}_summary.json")
    with open(p) as f:
        return int(json.load(f)["N_bars"])


def load_w_n(setup):
    p = os.path.join(WEEKLY_STAGE_A_DIR, f"{setup}_stage_a.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        d = json.load(f)
    return int(d.get("W_N") or d.get("w_n") or 0) or None


def load_exit_rule(setup):
    """Top condition from the pointer file per setup."""
    p = os.path.join(SIGNAL_EXIT_DIR, f"signal_exit_{setup}.json")
    with open(p) as f:
        d = json.load(f)
    top = d["examples"][0] if False else None
    # Fields: 'top_conditions' preferred; fall back to 'examples'[0] or first in d.
    for key in ("top_conditions", "candidates", "conditions"):
        if key in d and isinstance(d[key], list) and d[key]:
            top = d[key][0]
            break
    if top is None:
        # The discovered structure has the top at d['examples'][0] in practice; but the
        # display above showed d keys include 'examples' as a list of entries per example.
        # The actual top condition is the first candidate. Inspect carefully:
        # structure per early check: d has keys including 'examples'; top condition lives
        # at d['candidates'][0] OR the JSON uses 'top_conditions'. Try generic scan:
        for v in d.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "expression" in v[0]:
                top = v[0]
                break
    if top is None:
        raise RuntimeError(f"could not locate top condition in {p}")
    return {"expr": top["expression"], "op": top["direction"], "thr": float(top["threshold"])}


def load_presignal_pre_dedup(setup):
    p = os.path.join(PRESIGNAL_SCAN_DIR, f"{setup}_passes.pkl")
    with open(p, "rb") as f:
        d = pickle.load(f)
    return d["pre_dedup_by_ticker"]


def load_expr_vocab():
    with open(os.path.join(CACHE, "expr_series", "_manifest.json")) as f:
        m = json.load(f)
    return {n: i for i, n in enumerate(m["expr_names"])}


# ─────────────────────── per-example computations ───────────────────────

def dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def compute_adr14(df, at_idx):
    """ADR14 at bar at_idx = mean(high-low) over the 14 bars ending at at_idx inclusive.
    Falls back to fewer bars if history is short (min 1 bar)."""
    lo = max(0, at_idx - 13)
    h = df["high"].values[lo:at_idx + 1].astype(float)
    l = df["low"].values[lo:at_idx + 1].astype(float)
    return float(np.mean(h - l))


def cluster_run_backward(pre_idxs_set, sig_idx):
    """Walk backward from sig_idx through contiguous bars present in pre_idxs_set.
    Returns sorted list of bar indices including sig_idx."""
    run = [sig_idx]
    k = sig_idx - 1
    while k in pre_idxs_set:
        run.append(k)
        k -= 1
    run.reverse()
    return run


def earnings_cap_offset(ern_arr, dates_arr, sig_idx):
    """Bars from sig_idx to min(earnings-1, sig+MAX_FORWARD, len(df)-1)."""
    df_end_off = len(dates_arr) - 1 - sig_idx
    cap_off = min(MAX_FORWARD, df_end_off)
    if ern_arr is None or len(ern_arr) == 0:
        return cap_off
    sig_date = dates_arr[sig_idx]
    pos = int(np.searchsorted(ern_arr, sig_date, side="right"))
    if pos >= len(ern_arr):
        return cap_off
    ern_date = ern_arr[pos]
    ern_bar = int(np.searchsorted(dates_arr, ern_date, side="left"))
    if ern_bar <= sig_idx:
        return cap_off
    earnings_cap = ern_bar - 1 - sig_idx
    return max(0, min(cap_off, earnings_cap))


def locate_expr_fire(cache_dates, cache_data, expr_col, op, thr, sig_idx_in_cache, max_off):
    """Walk forward in expr cache from sig+1. Returns offset (>=1) of first bar where
    `op(value, thr)` is True and value is non-NaN, else None."""
    if expr_col is None:
        return None
    end = sig_idx_in_cache + max_off
    if end >= len(cache_dates):
        end = len(cache_dates) - 1
    for k in range(sig_idx_in_cache + 1, end + 1):
        v = float(cache_data[k, expr_col])
        if np.isnan(v):
            continue
        hit = (op == "<=" and v <= thr) or (op == ">=" and v >= thr) \
              or (op == "<" and v < thr) or (op == ">" and v > thr)
        if hit:
            return k - sig_idx_in_cache
    return None


def build_panel_for_setup(setup, universe, ern_map, expr_name2idx):
    direction = DIRECTION[setup]
    include_pivots = (direction == +1)  # key levels + breach cols are breakout-scope only
    n_bars = load_n_bars(setup)
    w_n = load_w_n(setup)
    v3_window = (w_n * 5) if w_n else n_bars  # weekly-as-daily approx
    exit_rule = load_exit_rule(setup)
    pre_dedup = load_presignal_pre_dedup(setup)
    examples = load_db_examples(setup)

    print(f"\n=== {setup} === direction={direction:+d}  examples_in_db={len(examples)}  N_bars={n_bars}  "
          f"V3_window={v3_window}  exit_rule={exit_rule['expr']} {exit_rule['op']} {exit_rule['thr']}",
          flush=True)

    expr_col = expr_name2idx.get(exit_rule["expr"])
    if expr_col is None:
        raise RuntimeError(f"{setup}: exit expr {exit_rule['expr']} not in cache vocab")

    scalars_rows = []
    ts_rows = []
    unresolvable = []

    for ex in examples:
        ticker, entry_date = ex["ticker"], ex["entry_date"]
        df = universe.get(ticker)
        if df is None:
            unresolvable.append((ticker, entry_date, "no_df")); continue
        ds = dates_str(df)
        m = np.where(ds == entry_date)[0]
        if len(m) == 0:
            unresolvable.append((ticker, entry_date, "entry_date_not_in_ohlcv")); continue
        entry_idx = int(m[0])
        sig_idx = entry_idx - 1
        if sig_idx < 0:
            unresolvable.append((ticker, entry_date, "sig_idx<0")); continue

        sig_date = ds[sig_idx]
        sig_open = float(df["open"].values[sig_idx])
        sig_high = float(df["high"].values[sig_idx])
        sig_low = float(df["low"].values[sig_idx])
        sig_close = float(df["close"].values[sig_idx])
        adr = compute_adr14(df, sig_idx)
        if adr <= 0 or np.isnan(adr):
            unresolvable.append((ticker, entry_date, "adr<=0")); continue

        # Key levels (breakout-scope only; fades skip per classifier scope discipline)
        if include_pivots:
            v1_lo = max(0, sig_idx - n_bars + 1)
            v1 = float(np.max(df["high"].values[v1_lo:sig_idx + 1]))

            pre_set = set(pre_dedup.get(ticker, []))
            if sig_idx in pre_set:
                run = cluster_run_backward(pre_set, sig_idx)
                v2 = float(np.max(df["high"].values[run[0]:run[-1] + 1]))
            else:
                v2 = sig_high  # fallback; shouldn't happen for examples

            v3_lo = max(0, sig_idx - v3_window + 1)
            v3 = float(np.max(df["high"].values[v3_lo:sig_idx + 1]))

            v4 = float(np.max(df["close"].values[v1_lo:sig_idx + 1]))

            if sig_idx - 1 >= v1_lo:
                v5 = float(np.max(df["high"].values[v1_lo:sig_idx]))  # exclude sig_idx
            else:
                v5 = float("nan")

        # Earnings cap
        earn_cap_off = earnings_cap_offset(ern_map.get(ticker), ds, sig_idx)

        # Exit fire offset — requires expr cache. Map sig_date -> cache idx.
        cache_dates, cache_data = load_ticker_cache(ticker, cast_to_float32=False)
        if cache_dates is None:
            unresolvable.append((ticker, entry_date, "expr_cache_missing")); continue
        c_ds = np.array([str(x)[:10] for x in cache_dates])
        cm = np.where(c_ds == sig_date)[0]
        if len(cm) == 0:
            unresolvable.append((ticker, entry_date, "sig_date_not_in_expr_cache")); continue
        sig_cache_idx = int(cm[0])

        fire_off = locate_expr_fire(
            cache_dates, cache_data, expr_col, exit_rule["op"], exit_rule["thr"],
            sig_cache_idx, earn_cap_off,
        )

        # Scalar row
        scalar = {
            "setup": setup,
            "ticker": ticker,
            "entry_date": entry_date,
            "signal_date": sig_date,
            "signal_bar_idx": int(sig_idx),
            "entry_bar_idx": int(entry_idx),
            "sig_open": sig_open, "sig_high": sig_high, "sig_low": sig_low, "sig_close": sig_close,
            "adr_at_signal": adr,
            "direction": int(direction),
            "earnings_cap_offset": int(earn_cap_off),
            "exit_fire_offset": int(fire_off) if fire_off is not None else None,
        }
        if include_pivots:
            scalar.update({"v1": v1, "v2": v2, "v3": v3, "v4": v4, "v5": v5})
        scalars_rows.append(scalar)

        # Time-series rows
        cum_max_high = -np.inf
        cum_min_low = np.inf
        exit_hit = False
        exit_fire_bar = sig_idx + fire_off if fire_off is not None else None
        if include_pivots:
            sighigh_hit = False
            siglow_hit = False
            v_hits = [False, False, False, False, False]
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
            if row_exit:
                exit_hit = True

            row = {
                "setup": setup, "ticker": ticker, "entry_date": entry_date,
                "offset": off,
                "date": ds[bar],
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
                "exit_fire": row_exit,
                "exit_fired_by_here": exit_hit,
                "past_earnings_cap": off == earn_cap_off,
            }
            if include_pivots:
                row_sighigh = (not sighigh_hit) and (h > sig_high)
                if row_sighigh:
                    sighigh_hit = True
                row_siglow = (not siglow_hit) and (l < sig_low)
                if row_siglow:
                    siglow_hit = True
                row_vs = []
                for i, lv in enumerate(levels):
                    fire = False
                    if (not v_hits[i]) and (not np.isnan(lv)) and (h > lv):
                        v_hits[i] = True
                        fire = True
                    row_vs.append(fire)
                row.update({
                    "run_up_adr": (cum_max_high - sig_close) / adr,
                    "drawdown_adr": (cum_min_low - sig_close) / adr,
                    "sighigh_breach": row_sighigh,
                    "sighigh_breached_by_here": sighigh_hit,
                    "siglow_breach": row_siglow,
                    "siglow_breached_by_here": siglow_hit,
                    "v1_breach": row_vs[0], "v1_breached_by_here": v_hits[0],
                    "v2_breach": row_vs[1], "v2_breached_by_here": v_hits[1],
                    "v3_breach": row_vs[2], "v3_breached_by_here": v_hits[2],
                    "v4_breach": row_vs[3], "v4_breached_by_here": v_hits[3],
                    "v5_breach": row_vs[4], "v5_breached_by_here": v_hits[4],
                })
            ts_rows.append(row)

    scalars = pd.DataFrame(scalars_rows)
    timeseries = pd.DataFrame(ts_rows)

    return scalars, timeseries, unresolvable


# ─────────────────────── summaries ───────────────────────

def first_look_summary(setup, scalars, timeseries):
    """Produce distribution snapshots useful for eyeballing structure."""
    s = scalars
    direction = int(s["direction"].iloc[0]) if "direction" in s.columns and len(s) else DIRECTION.get(setup, +1)
    include_pivots = (direction == +1)
    out = {
        "setup": setup,
        "direction": direction,
        "n_examples": len(s),
        "adr_at_signal_quartiles": [float(q) for q in np.quantile(s["adr_at_signal"], [0.1, 0.25, 0.5, 0.75, 0.9])],
        "earnings_cap_offset_quartiles": [int(q) for q in np.quantile(s["earnings_cap_offset"], [0.1, 0.25, 0.5, 0.75, 0.9])],
        "exit_fire_offset": {
            "n_fired": int(s["exit_fire_offset"].notna().sum()),
            "n_no_fire": int(s["exit_fire_offset"].isna().sum()),
            "quartiles_when_fired": [int(q) for q in np.quantile(s["exit_fire_offset"].dropna(), [0.1, 0.25, 0.5, 0.75, 0.9])] if s["exit_fire_offset"].notna().any() else None,
        },
    }
    if include_pivots:
        out["key_levels_above_sig_high_adr"] = {
            f"v{i}": {
                "median": float(np.nanmedian((s[f"v{i}"] - s["sig_high"]) / s["adr_at_signal"])),
                "quartiles": [float(q) for q in np.nanquantile((s[f"v{i}"] - s["sig_high"]) / s["adr_at_signal"], [0.1, 0.25, 0.5, 0.75, 0.9])],
            }
            for i in (1, 2, 3, 4, 5)
        }

        if len(timeseries) > 0:
            offsets_of_interest = [1, 2, 3, 5, 10, 20, 40, 60]
            rud = {}
            for off in offsets_of_interest:
                sub = timeseries[timeseries["offset"] == off]
                if len(sub) == 0:
                    continue
                rud[off] = {
                    "n": int(len(sub)),
                    "run_up_adr_quartiles": [float(q) for q in np.quantile(sub["run_up_adr"], [0.1, 0.25, 0.5, 0.75, 0.9])],
                    "drawdown_adr_quartiles": [float(q) for q in np.quantile(sub["drawdown_adr"], [0.1, 0.25, 0.5, 0.75, 0.9])],
                }
            out["run_up_drawdown_by_offset"] = rud

            breach_cum = {}
            for off in offsets_of_interest:
                sub = timeseries[timeseries["offset"] == off]
                if len(sub) == 0:
                    continue
                breach_cum[off] = {
                    "sighigh": float(sub["sighigh_breached_by_here"].mean()),
                    "siglow": float(sub["siglow_breached_by_here"].mean()),
                    "v1": float(sub["v1_breached_by_here"].mean()),
                    "v2": float(sub["v2_breached_by_here"].mean()),
                    "v3": float(sub["v3_breached_by_here"].mean()),
                    "v4": float(sub["v4_breached_by_here"].mean()),
                    "v5": float(sub["v5_breached_by_here"].mean()),
                    "exit": float(sub["exit_fired_by_here"].mean()),
                }
            out["cumulative_breach_rates_by_offset"] = breach_cum
    else:
        if len(timeseries) > 0:
            offsets_of_interest = [1, 2, 3, 5, 10, 20, 40, 60]
            exit_by_off = {}
            for off in offsets_of_interest:
                sub = timeseries[timeseries["offset"] == off]
                if len(sub) == 0:
                    continue
                exit_by_off[off] = {
                    "n": int(len(sub)),
                    "exit_fired_cum": float(sub["exit_fired_by_here"].mean()),
                }
            out["exit_cumulative_by_offset"] = exit_by_off
    return out


# ─────────────────────── main ───────────────────────

def main():
    assert OUT_DIR.startswith(WORKTREE), f"refusing write outside worktree: {OUT_DIR}"
    os.makedirs(OUT_DIR, exist_ok=True)

    print("Loading universe OHLCV...", flush=True)
    universe = load_universe()
    print(f"  tickers: {len(universe):,}", flush=True)
    assert len(universe) > 11000, f"universe size {len(universe)} looks wrong"

    print("Loading earnings...", flush=True)
    ern_map = load_earnings_map()
    print(f"  tickers with earnings: {len(ern_map):,}", flush=True)

    print("Loading expression vocab...", flush=True)
    expr_name2idx = load_expr_vocab()
    print(f"  expressions: {len(expr_name2idx):,}", flush=True)

    summaries = []
    for setup in SETUPS_TO_RUN:
        scalars, ts, unresolvable = build_panel_for_setup(setup, universe, ern_map, expr_name2idx)
        if unresolvable:
            print(f"  !! {setup} unresolvable: {len(unresolvable)} — {unresolvable[:5]}")

        s_path = os.path.join(OUT_DIR, f"{setup}_scalars.pkl")
        ts_path = os.path.join(OUT_DIR, f"{setup}_timeseries.pkl")
        assert s_path.startswith(WORKTREE)
        assert ts_path.startswith(WORKTREE)
        scalars.to_pickle(s_path)
        ts.to_pickle(ts_path)
        print(f"  wrote {len(scalars)} scalar rows -> {os.path.basename(s_path)}", flush=True)
        print(f"  wrote {len(ts):,} timeseries rows -> {os.path.basename(ts_path)}", flush=True)

        summ = first_look_summary(setup, scalars, ts)
        summ["unresolvable"] = [list(x) for x in unresolvable]
        summaries.append(summ)

    summary_path = os.path.join(OUT_DIR, "summary.json")
    assert summary_path.startswith(WORKTREE)
    with open(summary_path, "w") as f:
        json.dump({
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "setups": summaries,
        }, f, indent=2, default=str)
    print(f"\nwrote {summary_path}")


if __name__ == "__main__":
    main()
