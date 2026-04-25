"""Derive a signal-bar-only stop rule from the example data.

The rule must be anchored to quantities available at the signal bar (no entry-bar
reference), and must classify every example as a WIN.

For each example we know: signal bar, entry bar, actual entry extreme, exit time
(from the same exit expression the grinder uses).  Using that as ground truth we
measure how tightly various signal-bar-anchored stops can be placed without
stopping out any example inside its race window.

Candidate stop formulas (long — short mirrors):
  A)  stop = signal_low
  B)  stop = signal_low - 1.0 * ADR
  C)  stop = signal_close - 1.0 * ADR
  D)  stop = signal_low - 0.5 * ADR
  E)  stop = signal_close - 0.5 * ADR
  F)  stop = signal_low - K * ADR, K chosen per-setup as max across examples of
             (signal_low - deepest-low-between-signal+1-and-exit) / ADR, rounded.
  G)  Per-example: signal-low minus (max adverse excursion below signal_low
             measured from examples).  Same as F but computed across setups.

For every candidate we also apply the same race model:
  - race window = [signal+1, min(signal+120, last trading bar strictly before next earnings)]
  - LOSS if any low < stop (long) / high > stop (short) strictly BEFORE the exit fires
  - WIN otherwise

Report per setup:
  - per-example max adverse excursion below signal_low in ADR during race
     (this is the empirical floor on any signal-low-anchored rule)
  - for each candidate: count of example false negatives, non-example WR,
     distribution of stop distances from signal_close in ADR.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3
from collections import Counter
from statistics import median

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
sys.path.insert(0, os.path.join(MAIN_ROOT, "scripts"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache  # noqa: E402

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
EXIT_DIR = os.path.join(MAIN_ROOT, "data", "signal_exit_grind")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)


def latest_pyramid(setup):
    files = glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json"))
    files = [f for f in files if "sig0_pk0" not in os.path.basename(f)]
    with_ts = [(json.load(open(f))["timestamp"], f) for f in files]
    with_ts.sort()
    return with_ts[-1][1] if with_ts else None


def final_signals(pyr):
    last = []
    for _, tr in pyr["tier_results"].items():
        if tr.get("final_signals"):
            last = tr["final_signals"]
    return last


def load_exit(setup):
    p = os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")
    d = json.load(open(p))
    return d["top_conditions"][0]


def load_direction(setup):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT direction FROM setups WHERE setup_type=?", (setup,)).fetchone()
    return row[0]


def load_earnings_map():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    m = {}
    for tk, ed in rows:
        m.setdefault(tk, []).append(str(ed)[:10])
    return {tk: np.array(sorted(set(v))) for tk, v in m.items()}


def align_ohlcv(df, cd):
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        ohlcv_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    else:
        ohlcv_dates = np.array([str(d)[:10] for d in df["date"].values])
    expr_first = str(cd[0])[:10]
    hits = np.where(ohlcv_dates == expr_first)[0]
    if len(hits) == 0:
        return None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    return df_t if len(df_t) == len(cd) else None


def earnings_cap_bar(cache_dates, ern_sorted, sig_date):
    if ern_sorted is None or len(ern_sorted) == 0:
        return None
    pos = int(np.searchsorted(ern_sorted, sig_date, side="right"))
    if pos >= len(ern_sorted):
        return None
    ern = str(ern_sorted[pos])
    bp = int(np.searchsorted(np.asarray(cache_dates), ern, side="left"))
    return bp - 1 if bp > 0 else None


def adr_at(df_t, i):
    s = max(0, i - 13)
    h = df_t["high"].values[s:i + 1]
    l = df_t["low"].values[s:i + 1]
    return float(np.mean(h - l)) if len(h) else float("nan")


def find_exit_bar(exit_series, start, end, exit_dir, thresh):
    if start > end:
        return None
    s = exit_series[start:end + 1]
    fin = ~np.isnan(s)
    if exit_dir in (">=", "above"):
        hits = np.where(fin & (s >= thresh))[0]
    else:
        hits = np.where(fin & (s <= thresh))[0]
    return int(hits[0] + start) if len(hits) else None


def first_breach(lows_or_highs, start, end, stop_level, direction):
    if start > end:
        return None
    arr = lows_or_highs[start:end + 1]
    if direction == "long":
        hits = np.where(arr < stop_level)[0]
    else:
        hits = np.where(arr > stop_level)[0]
    return int(hits[0] + start) if len(hits) else None


def race_outcome(df_t, exit_series, signal_idx, direction, exit_dir, thresh,
                 stop_level, earnings_cap):
    """Return 'WIN' or 'LOSS' (plus reason + exit/stop indices)."""
    n = len(df_t)
    race_start = signal_idx + 1  # from the day after signal bar
    caps = [signal_idx + 120, n - 1]
    if earnings_cap is not None:
        caps.append(earnings_cap)
    race_end = min(caps)
    if race_start > race_end:
        return "WIN", "flat_before_earnings", None, None
    lows = df_t["low"].values
    highs = df_t["high"].values
    arr = lows if direction == "long" else highs
    first_stop = first_breach(arr, race_start, race_end, stop_level, direction)
    first_exit = find_exit_bar(exit_series, race_start, race_end, exit_dir, thresh)
    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return "LOSS", "stop_hit", first_exit, first_stop
    if first_exit is not None:
        return "WIN", "exit_fired", first_exit, first_stop
    if earnings_cap is not None and race_end == earnings_cap and race_end < signal_idx + 120:
        return "WIN", "flat_before_earnings", None, None
    return "WIN", "timeout", None, None


def mae_below_signal_low_per_example(df_t, signal_idx, direction, exit_bar_or_cap):
    """For a long, return max(signal_low - min(low[signal+1..exit])) in absolute price
    units.  For a short, max(max(high[signal+1..exit]) - signal_high).  If no bars,
    return 0.0 (no adverse excursion)."""
    lows = df_t["low"].values
    highs = df_t["high"].values
    start = signal_idx + 1
    end = exit_bar_or_cap
    if end is None or end < start:
        return 0.0
    if direction == "long":
        sl = float(lows[signal_idx])
        return sl - float(np.min(lows[start:end + 1]))
    else:
        sh = float(highs[signal_idx])
        return float(np.max(highs[start:end + 1])) - sh


def main():
    print("Load OHLCV + expr cache + earnings...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    expr_cache = ExprSeriesCache()
    earnings_map = load_earnings_map()
    print(f"  {len(universe):,} tickers, {expr_cache.n_expressions} exprs, "
          f"{len(earnings_map):,} tickers have earnings", flush=True)

    overall = {}
    per_example_rows = []

    for setup in ["dtss", "htf", "bf", "base"]:
        print(f"\n=== {setup.upper()} ===", flush=True)
        pyr = json.load(open(latest_pyramid(setup)))
        examples = pyr["example_signals"]
        direction = load_direction(setup)
        exit_cond = load_exit(setup)
        exit_col = expr_cache.expr_index(exit_cond["expression"])
        exit_dir = exit_cond["direction"]
        thresh = float(exit_cond["threshold"])
        print(f"  direction={direction}, n_examples={len(examples)}")

        # Build per-ticker prep once
        t_cache = {}
        for ex in examples:
            tk = ex["ticker"]
            if tk in t_cache:
                continue
            cd, cdata = expr_cache.get_ticker(tk)
            if cd is None:
                continue
            df = universe.get(tk)
            if df is None:
                continue
            df_t = align_ohlcv(df, cd)
            if df_t is None:
                continue
            cds = [str(d)[:10] for d in cd]
            t_cache[tk] = (df_t, cdata[:, exit_col], cds)

        # Per-example: measure MAE below signal_low during real race
        mae_adrs = []
        mae_vs_sigclose_adrs = []
        mae_signal_low_to_entry_adrs = []  # signal_low to entry_bar_low distance
        sig_low_breach_bars = []
        for ex in examples:
            tk = ex["ticker"]; sd = ex["date"]; ed = ex["entry_date"]
            if tk not in t_cache:
                continue
            df_t, es, cds = t_cache[tk]
            if sd not in cds or ed not in cds:
                continue
            sig_idx = cds.index(sd)
            ent_idx = cds.index(ed)
            adr = adr_at(df_t, sig_idx)
            if not np.isfinite(adr) or adr <= 0:
                continue
            ern_cap = earnings_cap_bar(cds, earnings_map.get(tk), sd)
            # Find exit bar using race model (honours earnings cap)
            n = len(df_t)
            caps = [sig_idx + 120, n - 1]
            if ern_cap is not None: caps.append(ern_cap)
            race_end = min(caps)
            exit_bar = find_exit_bar(es, sig_idx + 1, race_end, exit_dir, thresh)
            end_bar = exit_bar if exit_bar is not None else race_end
            mae_abs = mae_below_signal_low_per_example(df_t, sig_idx, direction, end_bar)
            mae_adrs.append(mae_abs / adr)
            sc = float(df_t["close"].values[sig_idx])
            if direction == "long":
                sl = float(df_t["low"].values[sig_idx])
                el = float(df_t["low"].values[ent_idx])
                mae_signal_low_to_entry_adrs.append((sl - el) / adr)  # +ve if entry_low < signal_low
                # worst low in race (below signal close)
                lows = df_t["low"].values[sig_idx + 1:end_bar + 1] if end_bar >= sig_idx + 1 else np.array([sc])
                mae_vs_sigclose_adrs.append((sc - float(np.min(lows))) / adr)
            else:
                sh = float(df_t["high"].values[sig_idx])
                eh = float(df_t["high"].values[ent_idx])
                mae_signal_low_to_entry_adrs.append((eh - sh) / adr)
                highs = df_t["high"].values[sig_idx + 1:end_bar + 1] if end_bar >= sig_idx + 1 else np.array([sc])
                mae_vs_sigclose_adrs.append((float(np.max(highs)) - sc) / adr)
            per_example_rows.append({
                "setup": setup, "ticker": tk, "signal_date": sd, "entry_date": ed,
                "direction": direction, "adr": round(adr, 3),
                "mae_from_signal_low_adr": round(mae_abs / adr, 3),
                "mae_from_signal_close_adr": round(mae_vs_sigclose_adrs[-1], 3),
                "entry_vs_signal_low_adr": round(mae_signal_low_to_entry_adrs[-1], 3),
                "exit_bar_rel": None if exit_bar is None else exit_bar - sig_idx,
            })

        if not mae_adrs:
            print("  no example stats"); continue

        def summ(xs):
            s = sorted(xs)
            return {
                "min": round(min(s), 2), "p10": round(s[int(0.10 * (len(s)-1))], 2),
                "p50": round(median(s), 2), "p90": round(s[int(0.90 * (len(s)-1))], 2),
                "max": round(max(s), 2),
            }
        print(f"  examples analyzed: {len(mae_adrs)}/{len(examples)}")
        print(f"  MAE from signal_low (ADR) during race:            {summ(mae_adrs)}")
        print(f"  MAE from signal_close (adverse) (ADR):            {summ(mae_vs_sigclose_adrs)}")
        print(f"  entry_extreme vs signal_extreme (ADR, +ve = entry past signal):")
        print(f"    {summ(mae_signal_low_to_entry_adrs)}")

        # Store summary for cross-setup view
        overall[setup] = {
            "direction": direction,
            "mae_from_signal_low_adr_max": round(max(mae_adrs), 3),
            "mae_from_signal_close_adr_max": round(max(mae_vs_sigclose_adrs), 3),
            "entry_past_signal_adr_max": round(max(mae_signal_low_to_entry_adrs), 3),
        }

    print("\n===== cross-setup summary =====")
    print(json.dumps(overall, indent=2))
    pd.DataFrame(per_example_rows).to_csv(os.path.join(OUT_DIR, "stop_derivation_examples.csv"), index=False)
    print(f"\nWrote stop_derivation_examples.csv ({len(per_example_rows)} rows)")


if __name__ == "__main__":
    main()
