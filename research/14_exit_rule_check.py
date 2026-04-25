"""E7 — Current exit rule re-check on full pop.

Run the same trichotomy race kit (entry=signal+1, stop=entry_extreme, ratchet N=10,
earnings cap via searchsorted) with each of the top 10 alternative exit expressions
from signal_exit_{setup}.json, scoring expectancy under the trichotomy on the full
deduped non-example population per setup.

Output: research/out/14_exit_alts_{setup}.csv + combined leaderboard.

Lives in dual-exit worktree (though it imports nothing from its scripts/).
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
TOP_N_ALTS = 10


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
    if len(hits) == 0: return None, None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != len(cd): return None, None
    return df_t, dates_full[off:end]


def earnings_cap_bar(ohlcv_dates, ern_sorted, entry_date_str):
    if ern_sorted is None or len(ern_sorted) == 0: return None
    pos = int(np.searchsorted(ern_sorted, entry_date_str, side="right"))
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


def race_one(direction, df, dates_str, entry_idx, adr, earnings_arr, entry_date,
             exit_series, exit_dir, exit_thresh, N):
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
    if race_end <= entry_idx: return None

    lows = df["low"].values; highs = df["high"].values; closes = df["close"].values
    stop = initial_stop; armed = False

    for i in range(entry_idx + 1, race_end + 1):
        bar_from_entry = i - entry_idx
        stop_hit = (lows[i] < stop) if direction == "long" else (highs[i] > stop)
        if stop_hit:
            if armed and stop == entry_close:
                return ("BE", 0.0)
            if direction == "long":
                return ("LOSS", (stop - entry_close) / adr)
            else:
                return ("LOSS", (entry_close - stop) / adr)
        v = exit_series[i]
        fires = False
        if not np.isnan(v):
            if exit_dir in (">=", "above") and v >= exit_thresh: fires = True
            elif exit_dir in ("<=", "below") and v <= exit_thresh: fires = True
        if fires:
            ec = float(closes[i])
            if direction == "long":
                return ("WIN", (ec - entry_close) / adr)
            else:
                return ("WIN", (entry_close - ec) / adr)
        if not armed and bar_from_entry >= N:
            ci = float(closes[i])
            if direction == "long" and ci > entry_close:
                stop = entry_close; armed = True
            elif direction == "short" and ci < entry_close:
                stop = entry_close; armed = True

    if had_earnings:
        fc = float(closes[race_end])
        m = (fc - entry_close) / adr if direction == "long" else (entry_close - fc) / adr
        outcome = "WIN" if m >= 0 else "LOSS"
        return (outcome, m)
    return ("LOSS", np.nan)


def main():
    print("=" * 60); print(f"E7 - Exit rule alternatives (top {TOP_N_ALTS} each)"); print("=" * 60)
    assert_paths_safe()

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    with sqlite3.connect(DB) as c:
        directions = dict(c.execute("SELECT setup_type, direction FROM setups").fetchall())
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()
        ern_rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()

    ern_map = defaultdict(list)
    for tk, ed in ern_rows: ern_map[tk].append(str(ed)[:10])
    ern_map = {tk: np.array(sorted(set(v))) for tk, v in ern_map.items()}
    example_dates = defaultdict(set)
    for s, t, d in ex_rows: example_dates[s].add((t, d))

    expr = ExprSeriesCache()
    adr_col = expr.expr_index("adr14")

    leaderboard = []

    for setup in ACTIVE:
        print(f"\n---- {setup.upper()} ----")
        with open(os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")) as f:
            exits_data = json.load(f)
        alts = exits_data["top_conditions"][:TOP_N_ALTS]
        print(f"  testing {len(alts)} alternatives")

        pyr_path = latest_pyramid(setup)
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        direction = directions[setup]
        klass = "fade" if setup in FADE_SETUPS else "breakout"

        # Pre-resolve tickers (only once per setup)
        t_cache = {}
        for s in sigs:
            tk = s["ticker"]
            if tk in t_cache: continue
            df = universe.get(tk)
            if df is None: t_cache[tk] = None; continue
            cd, cdata = expr.get_ticker(tk)
            if cd is None: t_cache[tk] = None; continue
            df_a, dates_a = align(df, cd)
            if df_a is None: t_cache[tk] = None; continue
            t_cache[tk] = (df_a, dates_a, cdata)

        # Build clusters (dedupe) once
        ticker_sigs = defaultdict(list)
        for s in sigs:
            tk = s["ticker"]; sd = s["date"]
            if t_cache.get(tk) is None: continue
            df_a, dates_a, _ = t_cache[tk]
            idx = np.where(dates_a == sd)[0]
            if len(idx) == 0: continue
            ticker_sigs[tk].append((int(idx[0]), sd))
        clusters = []
        for tk, lst in ticker_sigs.items():
            lst = sorted(set(lst))
            if not lst: continue
            i = 0
            while i < len(lst):
                j = i + 1
                while j < len(lst) and lst[j][0] == lst[j-1][0] + 1: j += 1
                clusters.append({"ticker": tk, "signal_idx": lst[j-1][0], "signal_date": lst[j-1][1]})
                i = j

        # Pre-compute race inputs per non-example cluster
        ex_sig_set = set()
        for t, d in example_dates[setup]:
            if t in t_cache and t_cache[t] is not None:
                df_a, dates_a, _ = t_cache[t]
                idx = np.where(dates_a == d)[0]
                if len(idx) > 0:
                    ex_sig_set.add((t, int(idx[0]) - 1))
        non_ex = []
        for c in clusters:
            if (c["ticker"], c["signal_idx"]) in ex_sig_set: continue
            tk = c["ticker"]; sig_idx = c["signal_idx"]; sig_date = c["signal_date"]
            df_a, dates_a, cdata = t_cache[tk]
            entry_idx = sig_idx + 1
            if entry_idx >= len(df_a) - 1: continue
            entry_date = dates_a[entry_idx]
            adr = float(cdata[entry_idx, adr_col]) if adr_col is not None else np.nan
            if not np.isfinite(adr) or adr <= 0:
                h = df_a["high"].values[max(0, entry_idx - 13):entry_idx + 1]
                l = df_a["low"].values[max(0, entry_idx - 13):entry_idx + 1]
                adr = float(np.mean(h - l)) if len(h) else float("nan")
            if not np.isfinite(adr) or adr <= 0: continue
            non_ex.append({
                "ticker": tk, "sig_date": sig_date, "entry_idx": entry_idx,
                "entry_date": entry_date, "adr": adr,
                "df": df_a, "dates": dates_a, "cdata": cdata,
                "earnings": ern_map.get(tk),
            })
        print(f"  non-example raceable: {len(non_ex)}")

        # For each alt exit, race all signals and compute summary
        setup_rows = []
        for rank, alt in enumerate(alts):
            col = expr.expr_index(alt["expression"])
            if col is None:
                print(f"  [{rank}] {alt['expression']}: MISSING FROM CACHE, skip")
                continue
            outcomes = []
            moves = []
            for r in non_ex:
                es = r["cdata"][:, col]
                out = race_one(direction, r["df"], r["dates"], r["entry_idx"], r["adr"],
                               r["earnings"], r["entry_date"], es, alt["direction"],
                               float(alt["threshold"]), RATCHET_N)
                if out is None: continue
                outcomes.append(out[0])
                moves.append(out[1])
            outcomes = np.array(outcomes)
            moves = np.array(moves, dtype=float)
            n = len(outcomes)
            if n == 0: continue
            w_mask = outcomes == "WIN"
            l_mask = outcomes == "LOSS"
            be_mask = outcomes == "BE"
            wr = w_mask.mean(); lr = l_mask.mean(); ber = be_mask.mean()
            mean_w = float(np.nanmean(moves[w_mask])) if w_mask.any() else 0.0
            mean_l = float(np.nanmean(moves[l_mask])) if l_mask.any() else 0.0
            exp_1adr = wr * mean_w - lr * 1.0
            exp_act = wr * mean_w + lr * mean_l
            setup_rows.append({
                "setup": setup, "class": klass, "alt_rank": rank,
                "expression": alt["expression"], "direction": alt["direction"], "threshold": alt["threshold"],
                "n": n, "wr": wr, "lr": lr, "ber": ber,
                "mean_w": mean_w, "mean_l": mean_l,
                "expectancy_1adr": exp_1adr, "expectancy_actual": exp_act,
            })

        df_sr = pd.DataFrame(setup_rows).sort_values("expectancy_1adr", ascending=False)
        path = os.path.join(OUT_DIR, f"14_exit_alts_{setup}.csv")
        df_sr.to_csv(path, index=False)
        print(f"  wrote {path}")

        # Show top-5 by expectancy_1adr
        print(f"  top 5 alternatives by expectancy (1-ADR convention):")
        print(f"    {'rank':>4} {'WR%':>5} {'BE%':>5} {'LR%':>5} {'meanW':>6} {'meanL':>6} {'E1':>7} {'Eact':>7}  expression")
        for _, r in df_sr.head(5).iterrows():
            marker = " <-CURRENT" if int(r['alt_rank']) == 0 else ""
            print(f"    {int(r['alt_rank']):>4} {r['wr']*100:>5.1f} {r['ber']*100:>5.1f} {r['lr']*100:>5.1f} {r['mean_w']:>6.2f} {r['mean_l']:>6.2f} {r['expectancy_1adr']:>+7.3f} {r['expectancy_actual']:>+7.3f}  {r['expression']}{marker}")

        leaderboard.extend(setup_rows)

    combined = pd.DataFrame(leaderboard)
    combined_path = os.path.join(OUT_DIR, "14_exit_alts_combined.csv")
    combined.to_csv(combined_path, index=False)
    print(f"\n  wrote {combined_path}")

    print()
    print("=" * 60)
    print("BEST EXIT PER SETUP (by expectancy_1adr)")
    print("=" * 60)
    for setup in ACTIVE:
        sub = combined[combined["setup"] == setup].sort_values("expectancy_1adr", ascending=False)
        if len(sub) == 0: continue
        best = sub.iloc[0]
        cur = sub[sub["alt_rank"] == 0]
        cur = cur.iloc[0] if len(cur) else None
        print(f"  {setup}:")
        print(f"    BEST  rank={int(best['alt_rank'])}  E1={best['expectancy_1adr']:+.3f}  Eact={best['expectancy_actual']:+.3f}  WR={best['wr']*100:.1f}%  {best['expression']}")
        if cur is not None and int(cur['alt_rank']) != int(best['alt_rank']):
            delta = best['expectancy_1adr'] - cur['expectancy_1adr']
            print(f"    cur   rank=0  E1={cur['expectancy_1adr']:+.3f}  Eact={cur['expectancy_actual']:+.3f}  WR={cur['wr']*100:.1f}%  (delta to best: {delta:+.3f})")


if __name__ == "__main__":
    main()
