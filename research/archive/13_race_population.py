"""E6 — Full population race, categorical labels per setup.

Applies the finalized kit to every non-example deduped signal per setup:
  entry = signal + 1 (grinder-construction convention)
  initial stop = entry_low (longs) / entry_high (shorts)
  ratchet: at bar 10 from entry, if close favorable, stop -> entry_close
  exit = signal_exit_{setup}.json top pick (close-based)
  earnings cap via np.searchsorted
  race_end = min(earnings_bar - 1, end_of_data)

Outcomes:
  WIN   = exit fired before stop hit
  BE    = stop = ratchet_entry_close was hit (only possible if ratchet armed)
  LOSS  = initial stop hit, OR earnings_flat_adverse, OR ran_out_of_data

Deduped signal source: pyramid_{setup}_mp_sig*.json tier_results.*final_signals.
Dedup: consecutive same-ticker bar indices form a cluster, rightmost bar is the
signal (matches pyramid_grinder's dedupe). Examples excluded via SQLite entry
dates.

Outputs research/out/13_population_labels_{setup}.csv + summary terminal print.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3
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
EXIT_DIR = os.path.join(MAIN_ROOT, "data", "signal_exit_grind")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

FADE_SETUPS = {"dtss", "3-4db"}
ACTIVE = ["htf", "bf", "base", "dtss"]
RATCHET_N = 10


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


def earnings_cap_bar(ohlcv_dates, ern_sorted, entry_date_str):
    if ern_sorted is None or len(ern_sorted) == 0:
        return None
    pos = int(np.searchsorted(ern_sorted, entry_date_str, side="right"))
    if pos >= len(ern_sorted):
        return None
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


def load_exit_cond(setup):
    with open(os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")) as f:
        return json.load(f)["top_conditions"][0]


def race_with_ratchet(direction, df, dates_str, entry_idx, adr,
                      earnings_arr, entry_date, exit_series, exit_dir, exit_thresh, N):
    if direction == "long":
        initial_stop = float(df["low"].values[entry_idx])
    else:
        initial_stop = float(df["high"].values[entry_idx])
    entry_close = float(df["close"].values[entry_idx])

    n = len(df)
    ern_bar = earnings_cap_bar(dates_str, earnings_arr, entry_date)
    race_end = n - 1
    had_earnings = 0
    if ern_bar is not None and ern_bar - 1 <= race_end:
        race_end = ern_bar - 1
        had_earnings = 1
    if race_end <= entry_idx:
        return None

    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values

    stop = initial_stop
    armed = False

    for i in range(entry_idx + 1, race_end + 1):
        bar_from_entry = i - entry_idx
        if direction == "long":
            stop_hit = lows[i] < stop
        else:
            stop_hit = highs[i] > stop
        if stop_hit:
            if armed and stop == entry_close:
                move_adr = 0.0
                return ("BE", "ratchet_hit", bar_from_entry, move_adr, initial_stop)
            if direction == "long":
                move_adr = (stop - entry_close) / adr
            else:
                move_adr = (entry_close - stop) / adr
            return ("LOSS", "stop_hit", bar_from_entry, move_adr, initial_stop)

        v = exit_series[i]
        fires = False
        if not np.isnan(v):
            if exit_dir in (">=", "above") and v >= exit_thresh: fires = True
            elif exit_dir in ("<=", "below") and v <= exit_thresh: fires = True
        if fires:
            exit_close = float(closes[i])
            if direction == "long":
                move_adr = (exit_close - entry_close) / adr
            else:
                move_adr = (entry_close - exit_close) / adr
            return ("WIN", "exit_fired", bar_from_entry, move_adr, initial_stop)

        if not armed and bar_from_entry >= N:
            close_i = float(closes[i])
            if direction == "long" and close_i > entry_close:
                stop = entry_close; armed = True
            elif direction == "short" and close_i < entry_close:
                stop = entry_close; armed = True

    if had_earnings:
        flat_close = float(closes[race_end])
        if direction == "long":
            move = (flat_close - entry_close) / adr
        else:
            move = (entry_close - flat_close) / adr
        if move > 0: outcome = "WIN"
        elif move < 0: outcome = "LOSS"
        else: outcome = "WIN"
        return (outcome, f"earnings_flat_{'favorable' if move>=0 else 'adverse'}", race_end - entry_idx, move, initial_stop)
    return ("LOSS", "ran_out_of_data", race_end - entry_idx, np.nan, initial_stop)


def main():
    print("=" * 60); print(f"E6 - Full pop race (ratchet N={RATCHET_N})"); print("=" * 60)
    assert_paths_safe()

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  OHLCV tickers: {len(universe):,}")

    with sqlite3.connect(DB) as c:
        directions = dict(c.execute("SELECT setup_type, direction FROM setups").fetchall())
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
        ern_rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    earnings_map = defaultdict(list)
    for tk, ed in ern_rows: earnings_map[tk].append(str(ed)[:10])
    earnings_map = {tk: np.array(sorted(set(v))) for tk, v in earnings_map.items()}

    # Build per-setup example entry date set
    example_dates = defaultdict(set)
    for s, t, d in ex_rows:
        example_dates[s].add((t, d))

    expr = ExprSeriesCache()
    adr_col = expr.expr_index("adr14")

    all_summary = {}

    for setup in ACTIVE:
        print(f"\n---- {setup.upper()} ----")
        pyr_path = latest_pyramid(setup)
        if pyr_path is None:
            print(f"  no pyramid file, skip")
            continue
        pyr = json.load(open(pyr_path))
        print(f"  pyramid: {os.path.basename(pyr_path)}")
        sigs = final_signals_from_pyramid(pyr)
        print(f"  raw signals: {len(sigs)}")

        direction = directions[setup]
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        ex_cond = load_exit_cond(setup)
        exit_col = expr.expr_index(ex_cond["expression"])
        if exit_col is None:
            print(f"  exit expr missing from cache, skip"); continue

        # Resolve signals to bar indices, group by ticker, dedupe consecutive
        ticker_sigs = defaultdict(list)  # ticker -> [(signal_bar_idx, signal_date)]
        t_cache = {}
        for s in sigs:
            tk = s["ticker"]; sd = s["date"]
            if tk not in t_cache:
                df = universe.get(tk)
                if df is None:
                    t_cache[tk] = None; continue
                cd, cdata = expr.get_ticker(tk)
                if cd is None:
                    t_cache[tk] = None; continue
                df_a, dates_a = align(df, cd)
                if df_a is None:
                    t_cache[tk] = None; continue
                t_cache[tk] = (df_a, dates_a, cdata)
            if t_cache[tk] is None: continue
            df_a, dates_a, _ = t_cache[tk]
            idx = np.where(dates_a == sd)[0]
            if len(idx) == 0: continue
            ticker_sigs[tk].append((int(idx[0]), sd))

        # Dedupe: sort by bar_idx, consecutive (idx+1) -> same cluster, take rightmost
        clusters = []
        for tk, lst in ticker_sigs.items():
            lst = sorted(set(lst))
            if not lst: continue
            i = 0
            while i < len(lst):
                j = i + 1
                while j < len(lst) and lst[j][0] == lst[j-1][0] + 1:
                    j += 1
                rightmost_idx, rightmost_date = lst[j-1]
                clusters.append({"ticker": tk, "signal_idx": rightmost_idx, "signal_date": rightmost_date})
                i = j

        # Build example signal_idx set per ticker (signal bar = entry-1 by convention)
        ex_sig_set = set()
        for t, d in example_dates[setup]:
            if t in t_cache and t_cache[t] is not None:
                df_a, dates_a, _ = t_cache[t]
                # entry date idx -> signal bar idx is entry - 1
                idx = np.where(dates_a == d)[0]
                if len(idx) > 0:
                    ex_sig_set.add((t, int(idx[0]) - 1))

        n_clusters = len(clusters)
        n_ex = sum(1 for c in clusters if (c["ticker"], c["signal_idx"]) in ex_sig_set)
        n_nonex = n_clusters - n_ex
        print(f"  clusters: {n_clusters}  (example-matched: {n_ex}, non-example: {n_nonex})")

        # Race each non-example cluster
        rows = []
        for c in clusters:
            tk = c["ticker"]; sig_idx = c["signal_idx"]; sig_date = c["signal_date"]
            is_example = (tk, sig_idx) in ex_sig_set
            df_a, dates_a, cdata = t_cache[tk]
            entry_idx = sig_idx + 1
            if entry_idx >= len(df_a) - 1:
                rows.append({
                    "setup": setup, "class": klass, "ticker": tk, "signal_date": sig_date,
                    "signal_idx": sig_idx, "entry_idx": entry_idx, "is_example": int(is_example),
                    "outcome": None, "reason": "entry_is_last_bar",
                    "bars": None, "move_adr": None, "entry_close": None, "stop": None, "adr": None,
                })
                continue
            entry_date = dates_a[entry_idx]

            adr = float(cdata[entry_idx, adr_col]) if adr_col is not None else np.nan
            if not np.isfinite(adr) or adr <= 0:
                h = df_a["high"].values[max(0, entry_idx - 13):entry_idx + 1]
                l = df_a["low"].values[max(0, entry_idx - 13):entry_idx + 1]
                adr = float(np.mean(h - l)) if len(h) else float("nan")
            if not np.isfinite(adr) or adr <= 0:
                rows.append({
                    "setup": setup, "class": klass, "ticker": tk, "signal_date": sig_date,
                    "signal_idx": sig_idx, "entry_idx": entry_idx, "is_example": int(is_example),
                    "outcome": None, "reason": "bad_adr",
                    "bars": None, "move_adr": None, "entry_close": None, "stop": None, "adr": None,
                })
                continue

            exit_series = cdata[:, exit_col]
            r = race_with_ratchet(direction, df_a, dates_a, entry_idx, adr,
                                  earnings_map.get(tk), entry_date,
                                  exit_series, ex_cond["direction"], float(ex_cond["threshold"]), RATCHET_N)
            if r is None:
                rows.append({
                    "setup": setup, "class": klass, "ticker": tk, "signal_date": sig_date,
                    "signal_idx": sig_idx, "entry_idx": entry_idx, "is_example": int(is_example),
                    "outcome": None, "reason": "no_race_window",
                    "bars": None, "move_adr": None, "entry_close": None, "stop": None, "adr": None,
                })
                continue
            outcome, reason, bars, move_adr, initial_stop = r
            rows.append({
                "setup": setup, "class": klass, "ticker": tk, "signal_date": sig_date,
                "signal_idx": sig_idx, "entry_idx": entry_idx, "is_example": int(is_example),
                "entry_close": float(df_a["close"].values[entry_idx]),
                "adr": adr, "stop": initial_stop,
                "outcome": outcome, "reason": reason, "bars": bars, "move_adr": move_adr,
            })

        df_out = pd.DataFrame(rows)
        path = os.path.join(OUT_DIR, f"13_population_labels_{setup}.csv")
        df_out.to_csv(path, index=False)
        print(f"  wrote {path}  rows={len(df_out)}")

        # Summary (non-example only)
        non_ex = df_out[df_out["is_example"] == 0]
        raced = non_ex[non_ex["outcome"].notna()]
        unresolved = non_ex[non_ex["outcome"].isna()]
        n = len(raced)
        if n == 0:
            print(f"  (no raced non-examples)"); continue
        n_w = int((raced["outcome"] == "WIN").sum())
        n_l = int((raced["outcome"] == "LOSS").sum())
        n_be = int((raced["outcome"] == "BE").sum())
        wr = n_w / n
        lr = n_l / n
        ber = n_be / n
        winners = raced[raced["outcome"] == "WIN"]
        losers = raced[raced["outcome"] == "LOSS"]
        mean_w = float(winners["move_adr"].mean()) if len(winners) else 0.0
        med_w = float(winners["move_adr"].median()) if len(winners) else 0.0
        mean_l = float(losers["move_adr"].mean()) if len(losers) else 0.0
        # Expectancy (trichotomy): W% × mean_winner_ADR - L% × 1 + BE% × 0
        expectancy_1adr = wr * mean_w - lr * 1.0
        # Alternate: use actual mean loser ADR
        expectancy_actual = wr * mean_w + lr * mean_l  # mean_l is negative

        print(f"  non-example raced: {n} (unresolved: {len(unresolved)})")
        print(f"    WIN:  {n_w} ({wr*100:5.1f}%)")
        print(f"    BE:   {n_be} ({ber*100:5.1f}%)")
        print(f"    LOSS: {n_l} ({lr*100:5.1f}%)")
        print(f"    mean_winner_ADR: {mean_w:.3f}   median: {med_w:.3f}")
        print(f"    mean_loser_ADR:  {mean_l:.3f}  (sanity: ~-1 for 1 ADR stop)")
        print(f"    expectancy (1-ADR loss convention): {expectancy_1adr:+.3f}")
        print(f"    expectancy (actual loser ADR):      {expectancy_actual:+.3f}")

        reason_breakdown = raced["reason"].value_counts().to_dict()
        print(f"    reasons: {reason_breakdown}")

        all_summary[setup] = {
            "n_raced": n, "wr": wr, "lr": lr, "ber": ber,
            "mean_w": mean_w, "mean_l": mean_l,
            "expectancy_1adr": expectancy_1adr,
            "expectancy_actual": expectancy_actual,
        }

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'setup':>6} {'n':>5} {'WR%':>6} {'BE%':>6} {'LR%':>6} {'meanW':>7} {'meanL':>7} {'E[1ADR]':>9} {'E[actL]':>9}")
    for s, v in all_summary.items():
        print(f"{s:>6} {v['n_raced']:>5} {v['wr']*100:>6.1f} {v['ber']*100:>6.1f} {v['lr']*100:>6.1f} {v['mean_w']:>7.3f} {v['mean_l']:>7.3f} {v['expectancy_1adr']:>+9.3f} {v['expectancy_actual']:>+9.3f}")


if __name__ == "__main__":
    main()
