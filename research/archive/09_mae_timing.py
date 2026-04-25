"""E2 — MAE timing distribution per class.

For each curated example, track maximum adverse excursion from entry_close
(in ADR units) on every bar of the race window, and record the bar index
where MAE peaked. Used to inform the E3 breakeven ratchet.

"Adverse" is direction-aware:
  long  : MAE = (entry_close - bar_low)  / adr (positive = adverse)
  short : MAE = (bar_high - entry_close) / adr (positive = adverse)

Also tracks how soon the trade was "past breakeven" by a bar-high/low measure
that could anchor a ratchet: for longs, first bar whose low >= entry_close is
"past-BE-safe"; for shorts, first bar whose high <= entry_close.

Output: research/out/09_mae_timing_{class}.csv + per_setup CSV.
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


def trace_one(direction, df, dates_str, entry_idx, adr, earnings_arr, entry_date,
              exit_series, exit_dir, exit_thresh):
    """Return MAE trace + summary for one example, race-bounded by same rules as E1."""
    if direction == "long":
        stop = float(df["low"].values[entry_idx])
    else:
        stop = float(df["high"].values[entry_idx])
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

    mae_peak_bar = None
    mae_peak_adr = -np.inf
    outcome_bar = None
    outcome = None
    first_past_be_bar = None

    for i in range(entry_idx + 1, race_end + 1):
        if direction == "long":
            adverse = (entry_close - lows[i]) / adr
            if lows[i] >= entry_close and first_past_be_bar is None:
                first_past_be_bar = i - entry_idx
            stop_hit = lows[i] < stop
        else:
            adverse = (highs[i] - entry_close) / adr
            if highs[i] <= entry_close and first_past_be_bar is None:
                first_past_be_bar = i - entry_idx
            stop_hit = highs[i] > stop

        if adverse > mae_peak_adr:
            mae_peak_adr = adverse
            mae_peak_bar = i - entry_idx

        if stop_hit:
            outcome = "LOSS"; outcome_bar = i - entry_idx
            break

        v = exit_series[i]
        if not np.isnan(v):
            if exit_dir in (">=", "above") and v >= exit_thresh:
                outcome = "WIN"; outcome_bar = i - entry_idx; break
            if exit_dir in ("<=", "below") and v <= exit_thresh:
                outcome = "WIN"; outcome_bar = i - entry_idx; break

    if outcome is None:
        if had_earnings:
            flat_close = float(closes[race_end])
            if direction == "long":
                move = (flat_close - entry_close) / adr
            else:
                move = (entry_close - flat_close) / adr
            outcome = "WIN" if move >= 0 else "LOSS"
            outcome_bar = race_end - entry_idx
        else:
            outcome = "LOSS"; outcome_bar = race_end - entry_idx

    return {
        "entry_idx": entry_idx, "entry_close": entry_close, "stop": stop, "adr": adr,
        "mae_peak_bar": mae_peak_bar, "mae_peak_adr": float(mae_peak_adr),
        "outcome": outcome, "outcome_bar": outcome_bar,
        "first_past_be_bar": first_past_be_bar, "had_earnings": had_earnings,
    }


def main():
    print("=" * 60); print("E2 — MAE timing per class"); print("=" * 60)
    assert_paths_safe()

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  OHLCV tickers: {len(universe):,}")

    with sqlite3.connect(DB) as c:
        directions = dict(c.execute("SELECT setup_type, direction FROM setups").fetchall())
        examples = [{"setup": s, "ticker": t, "entry_date": d} for s, t, d in c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss') ORDER BY setup_type, ticker, entry_date"
        ).fetchall()]
        ern_rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    earnings_map = defaultdict(list)
    for tk, ed in ern_rows:
        earnings_map[tk].append(str(ed)[:10])
    earnings_map = {tk: np.array(sorted(set(v))) for tk, v in earnings_map.items()}

    expr = ExprSeriesCache()
    adr_col = expr.expr_index("adr14")
    exit_by_setup = {}
    for s in ACTIVE:
        ec = load_exit_cond(s)
        col = expr.expr_index(ec["expression"])
        exit_by_setup[s] = {"cond": ec, "col": col}

    rows = []
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
        tr = trace_one(direction, df_a, dates_a, entry_idx, adr, earnings_map.get(ticker),
                       entry_date, exit_series, ex["cond"]["direction"], float(ex["cond"]["threshold"]))
        if tr is None: continue
        rows.append({
            "setup": setup, "class": klass, "ticker": ticker, "entry_date": entry_date,
            "direction": direction,
            **tr,
        })

    df_out = pd.DataFrame(rows)
    path_per_setup = os.path.join(OUT_DIR, "09_mae_timing_per_setup.csv")
    df_out.to_csv(path_per_setup, index=False)
    print(f"  Wrote {path_per_setup}  (n={len(df_out)})")

    for klass in ["breakout", "fade"]:
        sub = df_out[df_out["class"] == klass]
        path = os.path.join(OUT_DIR, f"09_mae_timing_{klass}.csv")
        sub.to_csv(path, index=False)
        print(f"  Wrote {path}  (n={len(sub)})")

    def pct(v, arr):
        return float(np.percentile(arr, v)) if len(arr) else float("nan")
    def frac_le(arr, v):
        return float(np.mean(arr <= v)) if len(arr) else float("nan")

    print()
    for klass in ["breakout", "fade"]:
        sub = df_out[df_out["class"] == klass]
        if len(sub) == 0: continue
        mae_bars = sub["mae_peak_bar"].dropna().values.astype(float)
        mae_adrs = sub["mae_peak_adr"].dropna().values.astype(float)
        be_bars = sub["first_past_be_bar"].dropna().values.astype(float)
        print(f"== {klass.upper()} (n={len(sub)}) ==")
        print(f"  MAE peak bar (from entry)  p10={pct(10,mae_bars):.1f}  p25={pct(25,mae_bars):.1f}  p50={pct(50,mae_bars):.1f}  p75={pct(75,mae_bars):.1f}  p90={pct(90,mae_bars):.1f}  max={mae_bars.max():.0f}")
        print(f"  MAE peak ADR               p50={pct(50,mae_adrs):.3f}  p90={pct(90,mae_adrs):.3f}  max={mae_adrs.max():.3f}")
        print(f"  Fraction MAE peak <= bar   1:{frac_le(mae_bars,1):.2f}  2:{frac_le(mae_bars,2):.2f}  3:{frac_le(mae_bars,3):.2f}  5:{frac_le(mae_bars,5):.2f}  10:{frac_le(mae_bars,10):.2f}")
        n_no_be = int(sub["first_past_be_bar"].isna().sum())
        print(f"  First past-BE bar          p50={pct(50,be_bars):.1f}  p90={pct(90,be_bars):.1f}  never-past-BE={n_no_be}/{len(sub)}")
        print()

    # Per-setup same stats
    print("-- Per setup --")
    for setup in ACTIVE:
        sub = df_out[df_out["setup"] == setup]
        if len(sub) == 0: continue
        mae_bars = sub["mae_peak_bar"].dropna().values.astype(float)
        mae_adrs = sub["mae_peak_adr"].dropna().values.astype(float)
        print(f"  {setup:>5}  n={len(sub):>3}  MAE_bar p50={pct(50,mae_bars):.1f} p90={pct(90,mae_bars):.1f}  MAE_adr p50={pct(50,mae_adrs):.3f} p90={pct(90,mae_adrs):.3f}  <=3bar:{frac_le(mae_bars,3):.2f}  <=5bar:{frac_le(mae_bars,5):.2f}")


if __name__ == "__main__":
    main()
