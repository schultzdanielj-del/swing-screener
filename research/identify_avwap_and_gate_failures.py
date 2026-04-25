"""Identify the breakout examples that fail the proposed AVWAP AND-gate.

Gate: for k in 1..10, close[sig+k] > close[sig] AND close[sig+k] > AVWAP(argmax_anchor..sig).
Failure: no k in 1..10 satisfies both clauses.

Output: ordered list of (setup, ticker, entry_date, sig_close, anchor_date, avwap_at_anchor, max_close_in_window).
"""
import os
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

with sqlite3.connect(DB) as conn:
    rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
    ).fetchall()

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def evaluate(setup, ticker, entry_date):
    if ticker not in universe:
        return None
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    dates = df["date"].dt.strftime("%Y-%m-%d").values
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    v = df["volume"].values.astype(float)
    tp = (h + l + c) / 3.0

    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        return None
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 1:
        return None

    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(0, sig_idx)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)
    argmax_A = int(A_range[int(np.argmax(avwaps))])
    avwap_val = float(avwaps.max())

    sig_close = float(c[sig_idx])

    # Forward window
    window_end = min(sig_idx + 10, len(df) - 1)
    if window_end <= sig_idx:
        return None
    forward_closes = c[sig_idx + 1: window_end + 1]
    forward_dates = dates[sig_idx + 1: window_end + 1]

    # AND-gate per k
    pass_close = forward_closes > sig_close
    pass_avwap = forward_closes > avwap_val
    pass_both = pass_close & pass_avwap

    any_pass = bool(pass_both.any())
    return {
        "setup": setup,
        "ticker": ticker,
        "entry_date": entry_date,
        "sig_close": sig_close,
        "anchor_date": dates[argmax_A],
        "anchor_bars_back": sig_idx - argmax_A,
        "avwap_anchor": avwap_val,
        "max_forward_close": float(forward_closes.max()),
        "passes_gate": any_pass,
    }


results = []
missing = []
for setup, ticker, entry_date in rows:
    r = evaluate(setup, ticker, entry_date)
    if r is None:
        missing.append((setup, ticker, entry_date))
        continue
    results.append(r)

failures = [r for r in results if not r["passes_gate"]]
passes = [r for r in results if r["passes_gate"]]

print(f"Total examples evaluated: {len(results)}")
print(f"Missing OHLCV: {len(missing)}")
print(f"Pass gate: {len(passes)}")
print(f"FAIL gate: {len(failures)}")
print()
print("=== FAILURES ===")
print(f"{'setup':<6} {'ticker':<8} {'entry_date':<12} {'sig_close':>10} {'anchor_date':<12} {'bars_back':>10} {'avwap':>10} {'max_fwd_close':>14}")
for r in failures:
    print(
        f"{r['setup']:<6} {r['ticker']:<8} {r['entry_date']:<12} "
        f"{r['sig_close']:>10.2f} {r['anchor_date']:<12} "
        f"{r['anchor_bars_back']:>10d} {r['avwap_anchor']:>10.2f} "
        f"{r['max_forward_close']:>14.2f}"
    )

# Tickers (for cross-check vs memory)
fail_tickers = [r["ticker"] for r in failures]
print()
print(f"Failure tickers: {sorted(fail_tickers)}")
