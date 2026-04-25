"""V1 test on all 4 active setups.

Flow per setup:
  1. Read latest pyramid_{setup}_mp_*.json (by 'timestamp' field).
  2. Pull final_signals from the last non-empty tier (= full deduplicated signal list
     whose count equals summary.final_total).
  3. Resolve each (ticker, date) to a cache-relative bar index.
  4. Tag example signal bars using example_signals from the pyramid file
     (entry_date - 1 trading day, resolved in cache dates).
  5. Group consecutive same-ticker bars into clusters; split at example signal bars so
     examples are always the rightmost of their own cluster.  Classification target =
     each cluster's rightmost bar.
  6. Apply V1:
        entry      = signal_idx + 1
        stop_level = low[entry] (long) / high[entry] (short)
        race       = [entry+1, min(signal_idx+120, end)]
        LOSS  first stop-breach bar (intraday low<stop | high>stop) at or before first
              exit-condition fire (close-evaluated).  Same-bar tie -> stop wins.
        WIN   first exit fire before any stop breach.
        WIN   neither fires inside 120 bars (timeout).
     No example override — report natural classification so we can see false negatives.

Report per setup:
  - n_signals_raw, n_clusters
  - n_examples_total (from pyramid.example_signals)
  - n_examples_matched_to_cluster (examples whose signal-idx is a cluster's rightmost)
  - n_examples_V1_win  (MUST equal n_examples_matched_to_cluster)
  - n_examples_V1_loss (MUST be zero; if non-zero -> rule is wrong for these)
  - non-example cluster win rate (informational only)
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3
from collections import Counter

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
    with_ts = []
    for f in files:
        with_ts.append((json.load(open(f))["timestamp"], f))
    with_ts.sort()
    return with_ts[-1][1] if with_ts else None


def final_signals(pyr):
    """Return the last non-empty tier's final_signals list."""
    last = None
    for tier, tr in pyr["tier_results"].items():
        if tr.get("final_signals"):
            last = tr["final_signals"]
    return last or []


def load_exit(setup):
    p = os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    tops = d.get("top_conditions")
    return tops[0] if tops else None


def load_direction(setup):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT direction FROM setups WHERE setup_type=?", (setup,)).fetchone()
    return row[0] if row else None


def load_earnings_map():
    """ticker -> sorted np.array of earnings_date strings (YYYY-MM-DD)."""
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    by_ticker = {}
    for tk, ed in rows:
        if ed is None:
            continue
        by_ticker.setdefault(tk, []).append(str(ed)[:10])
    for tk in by_ticker:
        by_ticker[tk] = np.array(sorted(set(by_ticker[tk])))
    return by_ticker


def earnings_cap_bar(cache_date_strs, ticker_earnings_sorted, signal_date_str):
    """Return the cache bar index of the last trading day STRICTLY BEFORE the next
    earnings date after signal_date_str, or None if no such earnings date.
    cache_date_strs: list of cache dates in ascending order (strings YYYY-MM-DD).
    ticker_earnings_sorted: sorted np.array of earnings date strings.
    """
    if ticker_earnings_sorted is None or len(ticker_earnings_sorted) == 0:
        return None
    # First earnings date STRICTLY AFTER signal_date
    pos = int(np.searchsorted(ticker_earnings_sorted, signal_date_str, side="right"))
    if pos >= len(ticker_earnings_sorted):
        return None
    ern = str(ticker_earnings_sorted[pos])
    # Last trading bar in cache with date < earnings date
    dates_arr = np.asarray(cache_date_strs)
    bp = int(np.searchsorted(dates_arr, ern, side="left"))  # first idx with date >= ern
    if bp == 0:
        return None  # earnings before the cache — impossible but safe
    return bp - 1


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


def classify_v1(df_t, exit_series, signal_idx, direction, exit_dir, exit_thresh,
                earnings_bar_cap=None, race_cap=120):
    """V1 classification. race_end = min(signal+race_cap, earnings_bar_cap, end_of_data).
    earnings_bar_cap = last trading bar STRICTLY BEFORE the next earnings date after the
    signal (None if no earnings ahead). Trader flat into prints.
    """
    n = len(df_t)
    if signal_idx + 1 >= n:
        return ("WIN", "no_entry_bar", None, None)
    entry = signal_idx + 1
    lows = df_t["low"].values
    highs = df_t["high"].values
    stop_level = float(lows[entry]) if direction == "long" else float(highs[entry])
    race_start = entry + 1

    caps = [signal_idx + race_cap, n - 1]
    if earnings_bar_cap is not None:
        caps.append(earnings_bar_cap)
    race_end = min(caps)

    if race_start > race_end:
        return ("WIN", "flat_before_earnings", None, None)

    lo = lows[race_start:race_end + 1]
    hi = highs[race_start:race_end + 1]
    if direction == "long":
        stop_hits = np.where(lo < stop_level)[0]
    else:
        stop_hits = np.where(hi > stop_level)[0]
    first_stop = int(stop_hits[0]) if len(stop_hits) else None

    ex_slice = exit_series[race_start:race_end + 1]
    finite = ~np.isnan(ex_slice)
    if exit_dir in (">=", "above"):
        ex_hits = np.where(finite & (ex_slice >= exit_thresh))[0]
    else:
        ex_hits = np.where(finite & (ex_slice <= exit_thresh))[0]
    first_exit = int(ex_hits[0]) if len(ex_hits) else None

    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return ("LOSS", "stop_hit", None, first_stop + (race_start - signal_idx))
    if first_exit is not None:
        return ("WIN", "exit_fired", first_exit + (race_start - signal_idx), None)
    # Neither fired inside the window. If earnings capped us, call it flat-pre-earnings.
    if earnings_bar_cap is not None and race_end == earnings_bar_cap and race_end < signal_idx + race_cap:
        return ("WIN", "flat_before_earnings", None, None)
    return ("WIN", "timeout", None, None)


def run_setup(setup, universe, expr_cache, earnings_map):
    print(f"\n=== {setup.upper()} ===", flush=True)
    pyr_path = latest_pyramid(setup)
    pyr = json.load(open(pyr_path))
    signals = final_signals(pyr)
    examples = pyr.get("example_signals", [])
    direction = load_direction(setup)
    exit_cond = load_exit(setup)
    exit_col = expr_cache.expr_index(exit_cond["expression"])
    print(f"  pyramid: {os.path.basename(pyr_path)}")
    print(f"  direction={direction}")
    print(f"  raw signals (pre-dedup): {len(signals)}")
    print(f"  examples:                 {len(examples)}")
    print(f"  exit: {exit_cond['expression']} {exit_cond['direction']} {exit_cond['threshold']}")

    # Resolve signal (ticker, date) -> cache bar_idx using per-ticker date map
    ticker_date_idx = {}   # ticker -> {date_str: cache_idx}
    ticker_cache_dates = {}  # ticker -> list of date strings (cache order)
    ticker_data = {}       # ticker -> (df_t, exit_series)
    unique_tickers = {s["ticker"] for s in signals} | {e["ticker"] for e in examples}
    for tk in unique_tickers:
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
        ticker_date_idx[tk] = {d: i for i, d in enumerate(cds)}
        ticker_cache_dates[tk] = cds
        ticker_data[tk] = (df_t, cdata[:, exit_col])

    # Build example_signal_set: (ticker, signal_idx) = (ticker, entry_idx-1)
    example_signal_set = {}
    for ex in examples:
        tk = ex["ticker"]; ed = ex["entry_date"]
        dmap = ticker_date_idx.get(tk, {})
        if ed not in dmap:
            continue
        eidx = dmap[ed]
        if eidx < 1:
            continue
        example_signal_set[(tk, eidx - 1)] = ex

    # Resolve all signals to (ticker, bar_idx)
    sig_bars = []
    unresolved = 0
    for s in signals:
        tk = s["ticker"]; sd = s["date"]
        dmap = ticker_date_idx.get(tk)
        if dmap is None or sd not in dmap:
            unresolved += 1
            continue
        sig_bars.append((tk, dmap[sd]))
    # Dedup + sort
    sig_bars = sorted(set(sig_bars))
    print(f"  resolved signal bars: {len(sig_bars)}  (unresolved: {unresolved})")

    # Cluster: consecutive same-ticker bar_idx; split at example signal bars
    clusters = []  # list of {ticker, rightmost_idx, size, is_example, example_entry_date}
    i = 0
    while i < len(sig_bars):
        j = i + 1
        tk = sig_bars[i][0]
        while j < len(sig_bars):
            if sig_bars[j][0] != tk:
                break
            if sig_bars[j][1] != sig_bars[j - 1][1] + 1:
                break
            if (tk, sig_bars[j - 1][1]) in example_signal_set:
                break
            j += 1
        rightmost_idx = sig_bars[j - 1][1]
        is_ex = (tk, rightmost_idx) in example_signal_set
        clusters.append({
            "ticker": tk,
            "rightmost_idx": rightmost_idx,
            "size": j - i,
            "is_example": is_ex,
            "example": example_signal_set.get((tk, rightmost_idx)),
        })
        i = j
    print(f"  clusters: {len(clusters)}")
    n_ex_clusters = sum(1 for c in clusters if c["is_example"])
    print(f"  clusters matched to an example: {n_ex_clusters}/{len(examples)}")

    # Classify each cluster's rightmost bar
    reason_counter = Counter()
    n_ex_win = n_ex_loss = 0
    n_nex_win = n_nex_loss = 0
    ex_losses = []
    nex_losses_sample = []

    for c in clusters:
        tk = c["ticker"]
        td = ticker_data.get(tk)
        if td is None:
            reason = "no_data"
            cls = "LOSS"
        else:
            df_t, ex_series = td
            # Earnings cap: last bar strictly before next earnings after signal_date
            sig_idx = c["rightmost_idx"]
            sig_date_str = ticker_cache_dates[tk][sig_idx]
            ern_cap = earnings_cap_bar(ticker_cache_dates[tk], earnings_map.get(tk), sig_date_str)
            cls, reason, _eb, _sb = classify_v1(
                df_t, ex_series, sig_idx, direction,
                exit_cond["direction"], float(exit_cond["threshold"]),
                earnings_bar_cap=ern_cap,
            )
        reason_counter[(cls, reason)] += 1
        if c["is_example"]:
            if cls == "WIN":
                n_ex_win += 1
            else:
                n_ex_loss += 1
                ex_losses.append({"ticker": tk, **(c["example"] or {}), "reason": reason})
        else:
            if cls == "WIN":
                n_nex_win += 1
            else:
                n_nex_loss += 1
                if len(nex_losses_sample) < 5:
                    nex_losses_sample.append({"ticker": tk, "signal_idx": c["rightmost_idx"], "reason": reason})

    print(f"\n  EXAMPLE CHECK (must all be WIN)")
    print(f"    V1_win:  {n_ex_win}/{n_ex_clusters}")
    print(f"    V1_loss: {n_ex_loss}")
    if ex_losses:
        print(f"    --- example FALSE NEGATIVES ---")
        for e in ex_losses[:20]:
            print(f"      {e['ticker']:<7s} signal={e.get('date')} entry={e.get('entry_date')} reason={e['reason']}")

    ne_total = n_nex_win + n_nex_loss
    print(f"\n  non-example clusters: {ne_total}  (WIN {n_nex_win}, LOSS {n_nex_loss})")
    if ne_total:
        print(f"  non-example win rate: {100*n_nex_win/ne_total:.1f}%")
    print(f"  reason breakdown: {dict(reason_counter)}")

    return {
        "setup": setup,
        "direction": direction,
        "pyramid_file": os.path.basename(pyr_path),
        "raw_signals": len(signals),
        "resolved_signal_bars": len(sig_bars),
        "unresolved": unresolved,
        "clusters": len(clusters),
        "examples_stored": len(examples),
        "clusters_matched_to_example": n_ex_clusters,
        "examples_V1_win": n_ex_win,
        "examples_V1_loss": n_ex_loss,
        "non_example_clusters": ne_total,
        "non_example_V1_win": n_nex_win,
        "non_example_V1_loss": n_nex_loss,
        "example_false_negatives": ex_losses,
    }


def main():
    print("Loading OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        u = pickle.load(f)
    print(f"  {len(u):,} tickers", flush=True)
    print("Loading expr cache...", flush=True)
    c = ExprSeriesCache()
    print("Loading earnings map...", flush=True)
    earnings_map = load_earnings_map()
    print(f"  earnings dates for {len(earnings_map):,} tickers", flush=True)
    rows = []
    for s in ["dtss", "htf", "bf", "base"]:
        rows.append(run_setup(s, u, c, earnings_map))

    print("\n===== SUMMARY =====")
    h = f"{'setup':6s} {'dir':5s} {'pyr_sigs':>9s} {'clusters':>9s} {'ex':>4s} {'ex_win':>7s} {'ex_loss':>8s} {'nex_win':>8s} {'nex_loss':>9s} {'nex_wr%':>8s}"
    print(h)
    for r in rows:
        ne = r["non_example_clusters"]
        wr = round(100 * r["non_example_V1_win"] / ne, 1) if ne else None
        print(f"{r['setup']:6s} {r['direction']:5s} "
              f"{r['raw_signals']:9d} {r['clusters']:9d} "
              f"{r['clusters_matched_to_example']:4d} "
              f"{r['examples_V1_win']:7d} {r['examples_V1_loss']:8d} "
              f"{r['non_example_V1_win']:8d} {r['non_example_V1_loss']:9d} "
              f"{wr!s:>8s}")

    with open(os.path.join(OUT_DIR, "v1_test.json"), "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nWrote {os.path.join(OUT_DIR, 'v1_test.json')}")


if __name__ == "__main__":
    main()
