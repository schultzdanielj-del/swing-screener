"""H8: Anchor = start of the most recent rally.

Approach:
  1. Walk backward from sig. Find the most recent IGNITE BAR I:
     - In window [sig - 250, sig - 1], find the bar with the largest (close[I] - close[I-1]) / ADR.
  2. Walk backward FROM I to find the rally's start:
     - The rally's start = the lowest close in [I - 14, I - 1].
  3. Anchor at that lowest close.

Test: match against Dan's 8 picks; AVWAP ratios; T1.1 across 113.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    ("AR",   "2020-12-17"): ("2020-12-11", 5.01),
    ("BB",   "2024-12-20"): ("2024-12-17", 3.07),
    ("DRN",  "2024-07-11"): ("2023-12-08", 8.54),
    ("HTT",  "2021-01-12"): ("2020-06-19", 1.69),
    ("LMND", "2024-11-05"): ("2024-11-01", 24.50),
    ("LMND", "2024-11-06"): ("2024-11-01", 24.51),
    ("PTON", "2024-10-14"): ("2024-09-20", 4.77),
    ("REAL", "2024-11-13"): ("2024-11-06", 3.79),
}

with sqlite3.connect(DB) as conn:
    rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
    ).fetchall()

with open(CACHE, "rb") as f:
    universe = pickle.load(f)


def load(ticker):
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
    return dates, h, l, c, v, tp


def adr14(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def avwap(tp, v, A, sig_idx):
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return float("nan")
    return float((tp[A:sig_idx+1] * seg_v).sum() / sv)


def find_ignite_anchor(c, sig_idx, adr, ignite_lookback=250, rally_window=14):
    """Find anchor = lowest close before most-recent ignite bar."""
    # Step 1: find most recent ignite bar I
    window_start = max(1, sig_idx - ignite_lookback)
    if window_start >= sig_idx: return None
    gains = (c[window_start:sig_idx] - c[window_start-1:sig_idx-1]) / adr
    # Most recent ignite = largest gain. Tie-break by recency (most recent index)
    # Find max
    max_gain_idx_local = int(np.argmax(gains))
    I = window_start + max_gain_idx_local
    # Step 2: lowest close in [I - rally_window, I - 1]
    rs = max(0, I - rally_window)
    if rs >= I: return None
    A = rs + int(np.argmin(c[rs:I]))
    return A, I


def evaluate_t1(c, sig_idx, avwap_val):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx: return False
    fwd = c[sig_idx+1:end+1]
    return bool(((fwd > sig_close) & (fwd > avwap_val)).any())


total = 0
t1_pass = 0
t1_fail = 0
no_anchor = 0
fail_details = []
records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 14: continue
    total += 1
    adr = adr14(h, l, sig_idx)
    result = find_ignite_anchor(c, sig_idx, adr)
    if result is None:
        no_anchor += 1
        continue
    A, I = result
    av = avwap(tp, v, A, sig_idx)
    if not np.isfinite(av):
        no_anchor += 1
        continue
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    max_fwd = float(c[sig_idx+1:end+1].max()) if end > sig_idx else float("nan")
    passes = evaluate_t1(c, sig_idx, av)
    if passes:
        t1_pass += 1
    else:
        t1_fail += 1
        fail_details.append((setup, ticker, entry_date,
            f"sig={sig_close:.2f} avw={av:.2f} max_fwd={max_fwd:.2f} bars_back={sig_idx-A} I={dates[I]}"))
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "A": A, "I": I,
        "anchor_date": dates[A], "ignite_date": dates[I],
        "sig_close": sig_close, "avwap": av,
        "passes": passes, "dates_arr": dates,
    })

print(f"=== T1.1 PASS RATE (rule = lowest close before most-recent ignite) ===")
print(f"Total: {total}")
print(f"Pass: {t1_pass}")
print(f"Fail: {t1_fail}")
print(f"No-anchor: {no_anchor}")
print()
if fail_details:
    print("=== FAILURES ===")
    for s, t, e, info in fail_details:
        print(f"  {s:<6} {t:<8} {e}  {info}")
print()

print("=== ANCHOR vs DAN PICK on 8 failures ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'ignite':<12} {'bars_off':>9} {'dan_avw':>8} {'rule_avw':>9} {'ratio':>6}")
bars_offs = []
ratios = []
for r in records:
    key = (r["ticker"], r["entry_date"])
    if key not in DAN_PICKS: continue
    dan_anchor_date, dan_avwap_val = DAN_PICKS[key]
    dan_a_idx = np.where(r["dates_arr"] == dan_anchor_date)[0]
    if len(dan_a_idx) == 0: continue
    dan_A = int(dan_a_idx[0])
    bars_off = r["A"] - dan_A
    ratio = r["avwap"] / dan_avwap_val if dan_avwap_val > 0 else float("nan")
    bars_offs.append(abs(bars_off))
    ratios.append(ratio)
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} {r['ignite_date']:<12} "
          f"{bars_off:>+9d} {dan_avwap_val:>8.2f} {r['avwap']:>9.2f} {ratio:>6.3f}")
if bars_offs:
    print(f"\n  >> mean abs bars off = {np.mean(bars_offs):.1f}")
    print(f"  >> median AVWAP ratio = {np.median(ratios):.3f}")
    print(f"  >> exact bar matches = {sum(1 for b in bars_offs if b == 0)} / {len(bars_offs)}")
    print(f"  >> within 5 bars = {sum(1 for b in bars_offs if b <= 5)} / {len(bars_offs)}")
