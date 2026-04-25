"""E3 — Breakeven ratchet design per class.

Ratchet rule (candidate): from entry+1 to entry+N, stop stays at entry_extreme.
If at or after bar N, the bar's close is on the favorable side of entry_close,
the stop moves to entry_close for all subsequent bars (BE ratchet armed).
Once armed, a bar that touches stop = entry_close is a BREAKEVEN outcome.

Sweep N ∈ {1, 2, 3, 4, 5, 7, 10, 15, 20} per class. For each N, race all
examples of that class and count W / BE / L. Gate: smallest N that yields 0 L.

Also report the 3 "never past BE" examples per class to see if they'd stop out
at entry_extreme anyway (i.e. they're the structural losses).
"""
from __future__ import annotations

import os, sys, json, pickle, sqlite3
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
N_VALUES = [1, 2, 3, 4, 5, 7, 10, 15, 20]


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


def load_exit_cond(setup):
    with open(os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")) as f:
        return json.load(f)["top_conditions"][0]


def race_with_ratchet(direction, df, dates_str, entry_idx, adr, earnings_arr, entry_date,
                      exit_series, exit_dir, exit_thresh, ratchet_N):
    """Race with ratchet. Returns (outcome, reason, bars_to_outcome, move_adr)."""
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
    ratchet_armed = False

    for i in range(entry_idx + 1, race_end + 1):
        bar_from_entry = i - entry_idx

        # Stop check (intraday)
        if direction == "long":
            stop_hit = lows[i] < stop
        else:
            stop_hit = highs[i] > stop
        if stop_hit:
            # If ratchet was armed and stop is now entry_close, it's a BE
            if ratchet_armed and stop == entry_close:
                if direction == "long":
                    move_adr = (stop - entry_close) / adr
                else:
                    move_adr = (entry_close - stop) / adr
                return ("BE", "ratchet_hit", bar_from_entry, move_adr)
            # Otherwise LOSS at initial stop
            if direction == "long":
                move_adr = (stop - entry_close) / adr
            else:
                move_adr = (entry_close - stop) / adr
            return ("LOSS", "stop_hit", bar_from_entry, move_adr)

        # Exit check (close-based)
        v = exit_series[i]
        exit_fires = False
        if not np.isnan(v):
            if exit_dir in (">=", "above") and v >= exit_thresh: exit_fires = True
            elif exit_dir in ("<=", "below") and v <= exit_thresh: exit_fires = True
        if exit_fires:
            exit_close = float(closes[i])
            if direction == "long":
                move_adr = (exit_close - entry_close) / adr
            else:
                move_adr = (entry_close - exit_close) / adr
            return ("WIN", "exit_fired", bar_from_entry, move_adr)

        # Arm ratchet AFTER stop/exit checks for this bar
        # (at or after bar N, if close is on favorable side of entry_close, raise stop)
        if not ratchet_armed and bar_from_entry >= ratchet_N:
            close_i = float(closes[i])
            if direction == "long" and close_i > entry_close:
                stop = entry_close
                ratchet_armed = True
            elif direction == "short" and close_i < entry_close:
                stop = entry_close
                ratchet_armed = True

    # No trigger
    if had_earnings:
        flat_close = float(closes[race_end])
        if direction == "long":
            move_adr = (flat_close - entry_close) / adr
        else:
            move_adr = (entry_close - flat_close) / adr
        if ratchet_armed and abs(move_adr) < 1e-9:
            outcome = "BE"
        elif move_adr > 0:
            outcome = "WIN"
        elif move_adr < 0:
            outcome = "LOSS"
        else:
            outcome = "WIN"  # exact zero -> WIN by convention
        return (outcome, f"earnings_flat_{'favorable' if move_adr>=0 else 'adverse'}", race_end - entry_idx, move_adr)
    return ("LOSS", "ran_out_of_data", race_end - entry_idx, np.nan)


def main():
    print("=" * 60); print("E3 — Breakeven ratchet sweep"); print("=" * 60)
    assert_paths_safe()

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)

    with sqlite3.connect(DB) as c:
        directions = dict(c.execute("SELECT setup_type, direction FROM setups").fetchall())
        examples = [{"setup": s, "ticker": t, "entry_date": d} for s, t, d in c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss') ORDER BY setup_type, ticker, entry_date"
        ).fetchall()]
        ern_rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    earnings_map = defaultdict(list)
    for tk, ed in ern_rows: earnings_map[tk].append(str(ed)[:10])
    earnings_map = {tk: np.array(sorted(set(v))) for tk, v in earnings_map.items()}

    expr = ExprSeriesCache()
    adr_col = expr.expr_index("adr14")
    exit_by_setup = {}
    for s in ACTIVE:
        ec = load_exit_cond(s)
        exit_by_setup[s] = {"cond": ec, "col": expr.expr_index(ec["expression"])}

    # Precompute per-example race inputs
    prepared = []
    for e in examples:
        setup = e["setup"]; ticker = e["ticker"]; entry_date = e["entry_date"]
        direction = directions[setup]
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        df = universe.get(ticker)
        if df is None: continue
        cd, cdata = expr.get_ticker(ticker)
        if cd is None: continue
        df_a, dates_a = align(df, cd)
        if df_a is None: continue
        if entry_date not in set(dates_a): continue
        entry_idx = int(np.where(dates_a == entry_date)[0][0])
        if entry_idx >= len(df_a) - 1: continue
        adr = float(cdata[entry_idx, adr_col]) if adr_col is not None else np.nan
        if not np.isfinite(adr) or adr <= 0:
            h = df_a["high"].values[max(0, entry_idx - 13):entry_idx + 1]
            l = df_a["low"].values[max(0, entry_idx - 13):entry_idx + 1]
            adr = float(np.mean(h - l)) if len(h) else float("nan")
        if not np.isfinite(adr) or adr <= 0: continue
        ex = exit_by_setup[setup]
        exit_series = cdata[:, ex["col"]]
        prepared.append({
            "setup": setup, "class": klass, "ticker": ticker, "entry_date": entry_date,
            "direction": direction, "df": df_a, "dates": dates_a, "entry_idx": entry_idx,
            "adr": adr, "earnings": earnings_map.get(ticker),
            "exit_series": exit_series, "exit_dir": ex["cond"]["direction"], "exit_thresh": float(ex["cond"]["threshold"]),
        })
    print(f"  Prepared {len(prepared)} examples")

    sweep_rows = []
    detail_rows = {klass: [] for klass in ["breakout", "fade"]}

    for N in N_VALUES:
        per_class_counts = {klass: defaultdict(int) for klass in ["breakout", "fade"]}
        per_class_losses = {klass: [] for klass in ["breakout", "fade"]}
        for p in prepared:
            r = race_with_ratchet(
                p["direction"], p["df"], p["dates"], p["entry_idx"], p["adr"],
                p["earnings"], p["entry_date"], p["exit_series"], p["exit_dir"], p["exit_thresh"], N,
            )
            if r is None: continue
            outcome, reason, bars, move = r
            per_class_counts[p["class"]][outcome] += 1
            if outcome == "LOSS" and reason != "ran_out_of_data":
                per_class_losses[p["class"]].append({
                    "N": N, "setup": p["setup"], "ticker": p["ticker"], "entry_date": p["entry_date"],
                    "reason": reason, "bars": bars, "move_adr": move,
                })

        for klass in ["breakout", "fade"]:
            cnts = per_class_counts[klass]
            total = sum(cnts.values())
            row = {
                "N": N, "class": klass, "n": total,
                "WIN": cnts.get("WIN", 0), "BE": cnts.get("BE", 0), "LOSS": cnts.get("LOSS", 0),
                "clear_LOSS": len([x for x in per_class_losses[klass] if x["reason"] != "ran_out_of_data"]),
            }
            sweep_rows.append(row)
            for lr in per_class_losses[klass]:
                detail_rows[klass].append(lr)

    sweep_df = pd.DataFrame(sweep_rows)
    sweep_path = os.path.join(OUT_DIR, "10_ratchet_sweep.csv")
    sweep_df.to_csv(sweep_path, index=False)
    print(f"  Wrote {sweep_path}")
    print()

    for klass in ["breakout", "fade"]:
        sub = sweep_df[sweep_df["class"] == klass]
        print(f"== {klass.upper()} ==")
        print(f"  {'N':>3}  {'n':>4}  {'WIN':>4}  {'BE':>3}  {'LOSS':>4}  {'clearL':>6}")
        for _, r in sub.iterrows():
            print(f"  {int(r['N']):>3}  {int(r['n']):>4}  {int(r['WIN']):>4}  {int(r['BE']):>3}  {int(r['LOSS']):>4}  {int(r['clear_LOSS']):>6}")
        # Smallest N with 0 clear losses
        ok = sub[sub["clear_LOSS"] == 0]
        if len(ok) > 0:
            best_n = int(ok.iloc[0]["N"])
            print(f"  >>> smallest N with 0 clear LOSSes: {best_n}")
        else:
            print(f"  >>> NO N value clears 0 clear LOSSes up to {max(N_VALUES)}")
        print()

    if any(detail_rows.values()):
        print("LOSS details (examples that remain LOSS for each N tested):")
        for klass in ["breakout", "fade"]:
            for d in detail_rows[klass][:40]:
                print(f"  {klass:>9}  N={d['N']:>3}  {d['setup']:>5}  {d['ticker']:>6}  {d['entry_date']}  {d['reason']}  bars={d['bars']}  move_adr={d['move_adr']:+.2f}")


if __name__ == "__main__":
    main()
