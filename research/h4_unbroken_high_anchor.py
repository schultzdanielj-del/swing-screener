"""H4: Anchor = most recent unbroken HIGH.

Rule: anchor = max A < sig such that high[A] > max(high[A+1..sig])
i.e., walking backward from sig-1, the first A whose high exceeds all later highs through sig.
This is the bar that defined the consolidation's ceiling — structurally the end of the
previous uptrend (its peak).
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


def avwap(tp, v, A, sig_idx):
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return float("nan")
    return float((tp[A:sig_idx+1] * seg_v).sum() / sv)


def find_unbroken_high(h, sig_idx):
    """Most recent A < sig such that high[A] > max(high[A+1..sig])."""
    if sig_idx < 1: return None
    running_max = h[sig_idx]
    for A in range(sig_idx - 1, -1, -1):
        if h[A] > running_max:
            return A
        if h[A] > running_max:
            running_max = h[A]
        else:
            running_max = max(running_max, h[A])
    return None


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
    if sig_idx < 1: continue
    total += 1
    A = find_unbroken_high(h, sig_idx)
    if A is None:
        no_anchor += 1
        fail_details.append((setup, ticker, entry_date, "no unbroken high"))
        continue
    av = avwap(tp, v, A, sig_idx)
    if not np.isfinite(av):
        no_anchor += 1
        fail_details.append((setup, ticker, entry_date, "NaN AVWAP"))
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
            f"sig={sig_close:.2f} avw={av:.2f} max_fwd={max_fwd:.2f} bars_back={sig_idx-A} anchor_high={h[A]:.2f}"))
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "A": A, "anchor_date": dates[A],
        "anchor_high": h[A],
        "sig_close": sig_close, "avwap": av,
        "passes": passes,
        "dates_arr": dates,
    })

print(f"=== T1.1 PASS RATE (rule = most recent unbroken HIGH) ===")
print(f"Total: {total}")
print(f"Pass: {t1_pass}")
print(f"Fail: {t1_fail}")
print(f"No-anchor: {no_anchor}")
print()
if fail_details:
    print("=== FAILURES + NO-ANCHOR CASES ===")
    for s, t, e, info in fail_details:
        print(f"  {s:<6} {t:<8} {e:<12}  {info}")
print()

print("=== ANCHOR vs DAN PICK on 8 failures ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'bars_off':>9} {'dan_avw':>8} {'rule_avw':>9} {'ratio':>6}")
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
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} "
          f"{bars_off:>+9d} {dan_avwap_val:>8.2f} {r['avwap']:>9.2f} {ratio:>6.3f}")
if bars_offs:
    print(f"\n  >> mean abs bars off = {np.mean(bars_offs):.1f}")
    print(f"  >> median AVWAP ratio = {np.median(ratios):.3f}")
    print(f"  >> exact bar matches = {sum(1 for b in bars_offs if b == 0)} / {len(bars_offs)}")
    print(f"  >> within 5 bars = {sum(1 for b in bars_offs if b <= 5)} / {len(bars_offs)}")
