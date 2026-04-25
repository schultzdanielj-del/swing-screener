"""E1 — Race validation on curated examples with worst-case stop.

For each of 192 curated examples across HTF / BF / BASE / DTSS:
  entry = recorded entry bar in scanperfect.db
  stop  = entry_low (long) or entry_high (short) — worst-case
  exit  = expression-cache condition from signal_exit_{setup}.json top_conditions[0]
  race  = intraday stop-check vs close-based exit-check, earnings-capped

Gate: 0 clear LOSSes per class. Any LOSSes surfaced for Dan's review, not patched.
Outputs research/out/08_race_v1_examples.csv inside this worktree.
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


def assert_paths_safe():
    out_resolved = os.path.abspath(OUT_DIR)
    if not out_resolved.startswith(os.path.abspath(WORKTREE)):
        raise SystemExit(f"ABORT: OUT_DIR {out_resolved} outside worktree {WORKTREE}")
    if not out_resolved.replace("\\", "/").endswith("/research/out"):
        raise SystemExit(f"ABORT: OUT_DIR {out_resolved} not under research/out")
    print(f"  OK OUT_DIR inside worktree: {out_resolved}")


def load_universe():
    path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"  Loading OHLCV: {path}")
    with open(path, "rb") as f:
        uni = pickle.load(f)
    print(f"  OHLCV tickers: {len(uni):,}")
    if len(uni) < 11000:
        raise SystemExit(f"ABORT: OHLCV ticker count {len(uni)} below 11000 floor")
    return uni


def load_setups_directions():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT setup_type, direction FROM setups").fetchall()
    return dict(rows)


def load_examples():
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss') ORDER BY setup_type, ticker, entry_date"
        ).fetchall()
    return [{"setup": s, "ticker": t, "entry_date": d} for s, t, d in rows]


def load_earnings_map():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    m = defaultdict(list)
    for tk, ed in rows:
        m[tk].append(str(ed)[:10])
    return {tk: np.array(sorted(set(v))) for tk, v in m.items()}


def load_exit_cond(setup):
    path = os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")
    with open(path) as f:
        d = json.load(f)
    return d["top_conditions"][0]


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def align_ohlcv_to_cache(df, cd):
    """Align OHLCV to expr cache's date range — same pattern as research/07."""
    dates_full = ohlcv_dates_str(df)
    cd_first = str(cd[0])[:10]
    hits = np.where(dates_full == cd_first)[0]
    if len(hits) == 0:
        return None, None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != len(cd):
        return None, None
    dates_aligned = dates_full[off:end]
    return df_t, dates_aligned


def earnings_cap_bar(ohlcv_dates, ern_sorted, entry_date_str):
    """First bar index strictly on/after next earnings date after entry.
    Returns int bar index or None if no future earnings in-range."""
    if ern_sorted is None or len(ern_sorted) == 0:
        return None
    pos = int(np.searchsorted(ern_sorted, entry_date_str, side="right"))
    if pos >= len(ern_sorted):
        return None
    ern = str(ern_sorted[pos])
    bp = int(np.searchsorted(ohlcv_dates, ern, side="left"))
    if bp <= 0:
        return None
    return bp


def race_one(ticker, entry_date, setup, direction, exit_cond,
             df, dates_str, cached_data, adr_col, exit_col, earnings_arr):
    if entry_date not in set(dates_str):
        return {"status": "skip", "reason": "entry_date_not_in_cache"}
    entry_idx = int(np.where(dates_str == entry_date)[0][0])
    n = len(df)
    if entry_idx >= n - 1:
        return {"status": "skip", "reason": "entry_is_last_bar"}

    # ADR at entry
    adr = float(cached_data[entry_idx, adr_col]) if adr_col is not None else np.nan
    if not np.isfinite(adr) or adr <= 0:
        h = df["high"].values[max(0, entry_idx - 13):entry_idx + 1]
        l = df["low"].values[max(0, entry_idx - 13):entry_idx + 1]
        adr = float(np.mean(h - l)) if len(h) else float("nan")
    if not np.isfinite(adr) or adr <= 0:
        return {"status": "skip", "reason": "bad_adr"}

    entry_close = float(df["close"].values[entry_idx])
    entry_low = float(df["low"].values[entry_idx])
    entry_high = float(df["high"].values[entry_idx])

    if direction == "long":
        stop = entry_low
        stop_dist = (entry_close - stop) / adr
    else:
        stop = entry_high
        stop_dist = (stop - entry_close) / adr

    # Earnings cap: we race up to bar (earnings_bar - 1), flat-close at that bar.
    ern_bar = earnings_cap_bar(dates_str, earnings_arr, entry_date)
    race_end = n - 1
    had_earnings = 0
    if ern_bar is not None and ern_bar - 1 <= race_end:
        race_end = ern_bar - 1
        had_earnings = 1

    if race_end <= entry_idx:
        return {"status": "skip", "reason": "no_forward_window_after_earnings"}

    exit_dir = exit_cond["direction"]
    exit_thresh = float(exit_cond["threshold"])

    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values
    es = cached_data[:, exit_col]

    for i in range(entry_idx + 1, race_end + 1):
        # Stop (intraday) — checked first so same-bar tie resolves as LOSS
        if direction == "long":
            stop_hit = lows[i] < stop
        else:
            stop_hit = highs[i] > stop
        if stop_hit:
            if direction == "long":
                move_adr = (stop - entry_close) / adr
            else:
                move_adr = (entry_close - stop) / adr
            return {
                "status": "done", "outcome": "LOSS", "reason": "stop_hit",
                "bars": i - entry_idx, "move_adr": move_adr,
                "entry_idx": entry_idx, "entry_close": entry_close,
                "adr": adr, "stop": stop, "stop_distance_adr": stop_dist,
                "had_earnings": had_earnings,
            }
        # Exit (close-based)
        v = es[i]
        if not np.isnan(v):
            if exit_dir in (">=", "above") and v >= exit_thresh:
                fire = 1
            elif exit_dir in ("<=", "below") and v <= exit_thresh:
                fire = 1
            else:
                fire = 0
            if fire:
                exit_close = float(closes[i])
                if direction == "long":
                    move_adr = (exit_close - entry_close) / adr
                else:
                    move_adr = (entry_close - exit_close) / adr
                return {
                    "status": "done", "outcome": "WIN", "reason": "exit_fired",
                    "bars": i - entry_idx, "move_adr": move_adr,
                    "entry_idx": entry_idx, "entry_close": entry_close,
                    "adr": adr, "stop": stop, "stop_distance_adr": stop_dist,
                    "had_earnings": had_earnings,
                }

    # No trigger in race window
    if had_earnings:
        flat_close = float(closes[race_end])
        if direction == "long":
            move_adr = (flat_close - entry_close) / adr
        else:
            move_adr = (entry_close - flat_close) / adr
        outcome = "WIN" if move_adr >= 0 else "LOSS"
        reason = "earnings_flat_favorable" if move_adr >= 0 else "earnings_flat_adverse"
        return {
            "status": "done", "outcome": outcome, "reason": reason,
            "bars": race_end - entry_idx, "move_adr": move_adr,
            "entry_idx": entry_idx, "entry_close": entry_close,
            "adr": adr, "stop": stop, "stop_distance_adr": stop_dist,
            "had_earnings": had_earnings,
        }
    # Ran out of data
    return {
        "status": "done", "outcome": "LOSS", "reason": "ran_out_of_data",
        "bars": race_end - entry_idx, "move_adr": np.nan,
        "entry_idx": entry_idx, "entry_close": entry_close,
        "adr": adr, "stop": stop, "stop_distance_adr": stop_dist,
        "had_earnings": had_earnings,
    }


def main():
    print("=" * 60)
    print("E1 — Race on examples, worst-case stop, close-based exits")
    print("=" * 60)

    assert_paths_safe()
    universe = load_universe()
    directions = load_setups_directions()
    print(f"  Setup directions: {directions}")
    examples = load_examples()
    n_by_setup = defaultdict(int)
    for e in examples:
        n_by_setup[e["setup"]] += 1
    print(f"  Example counts: {dict(n_by_setup)}  (expected htf 32 / bf 45 / base 42 / dtss 73)")

    earnings_map = load_earnings_map()
    print(f"  Earnings tickers: {len(earnings_map):,}")

    expr_cache = ExprSeriesCache()
    adr_col = expr_cache.expr_index("adr14")
    if adr_col is None:
        print("  adr14 not a cache column — will fall back to OHLCV 14-bar mean")
    else:
        print(f"  adr14 column: {adr_col}")

    exit_by_setup = {}
    for s in ACTIVE:
        ec = load_exit_cond(s)
        col = expr_cache.expr_index(ec["expression"])
        if col is None:
            raise SystemExit(f"ABORT: exit expression {ec['expression']} not in expression cache")
        exit_by_setup[s] = {"cond": ec, "col": col}
        print(f"  {s}: exit={ec['expression']} {ec['direction']} {ec['threshold']}  col={col}")
    print()

    rows = []
    ticker_cache = {}
    for e in examples:
        setup = e["setup"]
        ticker = e["ticker"]
        entry_date = e["entry_date"]
        direction = directions.get(setup)
        klass = "fade" if setup in FADE_SETUPS else "breakout"

        if ticker not in ticker_cache:
            df = universe.get(ticker)
            if df is None:
                ticker_cache[ticker] = None
            else:
                cd, cdata = expr_cache.get_ticker(ticker)
                if cd is None:
                    ticker_cache[ticker] = None
                else:
                    df_aligned, dates_aligned = align_ohlcv_to_cache(df, cd)
                    if df_aligned is None:
                        ticker_cache[ticker] = None
                    else:
                        ticker_cache[ticker] = (df_aligned, dates_aligned, cdata)
        entry = ticker_cache[ticker]
        row = {
            "setup": setup, "class": klass, "ticker": ticker,
            "entry_date": entry_date, "direction": direction,
        }
        if entry is None:
            row.update({"status": "skip", "reason": "ticker_missing_from_cache"})
            rows.append(row)
            continue

        df, dates_str, cached_data = entry
        exit_info = exit_by_setup[setup]
        result = race_one(
            ticker, entry_date, setup, direction, exit_info["cond"],
            df, dates_str, cached_data, adr_col, exit_info["col"],
            earnings_map.get(ticker),
        )
        row.update(result)
        rows.append(row)

    df_out = pd.DataFrame(rows)
    out_cols = [
        "setup", "class", "ticker", "entry_date", "direction",
        "entry_idx", "entry_close", "adr", "stop", "stop_distance_adr",
        "outcome", "reason", "bars", "move_adr", "had_earnings",
        "status",
    ]
    for c in out_cols:
        if c not in df_out.columns:
            df_out[c] = np.nan
    df_out = df_out[out_cols]
    out_path = os.path.join(OUT_DIR, "08_race_v1_examples.csv")
    df_out.to_csv(out_path, index=False)
    print(f"  Wrote {out_path}")
    print(f"  Rows: {len(df_out)}")

    # Summary
    done = df_out[df_out["status"] == "done"].copy() if "status" in df_out.columns else df_out.copy()
    skipped = df_out[df_out.get("status", "") == "skip"]
    print()
    print("-" * 60)
    print(f"Skipped (not raced): {len(skipped)}")
    if len(skipped):
        for _, r in skipped.iterrows():
            print(f"    {r['setup']:>5}  {r['ticker']:>6}  {r['entry_date']}  reason={r.get('reason','')}")

    print()
    print("Per-class counts:")
    for klass in ["breakout", "fade"]:
        sub = done[done["class"] == klass]
        n = len(sub)
        w = int((sub["outcome"] == "WIN").sum())
        l = int((sub["outcome"] == "LOSS").sum())
        rod = int((sub["reason"] == "ran_out_of_data").sum())
        l_real = l - rod
        print(f"  {klass:>9}  n={n:>3}  WIN={w:>3}  LOSS_real={l_real:>3}  LOSS_rod={rod:>2}")

    print()
    print("Per-setup counts:")
    for setup in ACTIVE:
        sub = done[done["setup"] == setup]
        n = len(sub)
        w = int((sub["outcome"] == "WIN").sum())
        l = int((sub["outcome"] == "LOSS").sum())
        rod = int((sub["reason"] == "ran_out_of_data").sum())
        l_real = l - rod
        print(f"  {setup:>5}  n={n:>3}  WIN={w:>3}  LOSS_real={l_real:>3}  LOSS_rod={rod:>2}")

    print()
    print("LOSS reason breakdown:")
    losses = done[done["outcome"] == "LOSS"]
    by_reason = losses["reason"].value_counts().to_dict()
    for rsn, cnt in by_reason.items():
        print(f"  {rsn:>28}: {cnt}")

    print()
    print("Clear-LOSS examples (excluding ran_out_of_data):")
    clear = losses[losses["reason"] != "ran_out_of_data"].sort_values(["setup", "reason", "ticker"])
    if len(clear) == 0:
        print("  (none)")
    else:
        for _, r in clear.iterrows():
            move = r.get("move_adr")
            mv_str = f"{move:+.2f}" if isinstance(move, float) and np.isfinite(move) else "nan"
            print(f"  {r['setup']:>5}  {r['ticker']:>6}  {r['entry_date']}  reason={r['reason']:>25}  move_adr={mv_str}  bars={r.get('bars','?')}")

    print()
    print("-" * 60)
    print(f"GATE: clear LOSSes per class:")
    for klass in ["breakout", "fade"]:
        sub = done[(done["class"] == klass) & (done["outcome"] == "LOSS") & (done["reason"] != "ran_out_of_data")]
        status = "PASS" if len(sub) == 0 else "FAIL"
        print(f"  {klass:>9}: {len(sub)}  [{status}]")


if __name__ == "__main__":
    main()
