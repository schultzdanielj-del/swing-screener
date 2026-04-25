"""H10: R3 with Dan's restricted MA candidate set.

MA candidates: SMA50, SMA100, SMA200, EMA3, EMA8, EMA21. Nothing else.
Pick longest-period MA whose distance from sig_close is within t_lso ADR.
R3 anchor: highest close in [sig - N_lso, sig - 1].
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

DAN_PICKS = {
    ("AR",   "2020-12-17"): ("2020-12-11", 5.01),
    ("BB",   "2024-12-20"): ("2024-12-17", 3.07),
    ("DRN",  "2024-07-11"): ("2023-12-08", 8.54),
    ("HTT",  "2021-01-12"): ("2020-06-19", 1.69),  # may be mislabeled (divergence pivot, not HTF)
    ("LMND", "2024-11-05"): ("2024-11-01", 24.50),
    ("LMND", "2024-11-06"): ("2024-11-01", 24.51),
    ("PTON", "2024-10-14"): ("2024-09-20", 4.77),
    ("REAL", "2024-11-13"): ("2024-11-06", 3.79),
}

# Dan-restricted MA set: SMA 50,100,200 + EMA 3,8,21
MA_SET = [
    ("EMA",   3),
    ("EMA",   8),
    ("EMA",  21),
    ("SMA",  50),
    ("SMA", 100),
    ("SMA", 200),
]

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


def sma_at(arr, n, idx):
    if idx < n - 1: return float("nan")
    return float(np.mean(arr[idx-n+1:idx+1]))


def ema_at(arr, n, idx):
    if idx < n - 1: return float("nan")
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx+1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)


def lso_period(c, sig_idx, adr, t_lso=2.0):
    sig_close = c[sig_idx]
    qualifying = []
    for kind, n in MA_SET:
        if sig_idx < n - 1: continue
        v = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
        if not np.isfinite(v): continue
        signed = (sig_close - v) / adr
        if abs(signed) <= t_lso:
            qualifying.append((n, kind, v, signed))
    if not qualifying:
        return None, None, None
    # Longest period; tie-break by closer
    qualifying.sort(key=lambda x: (-x[0], abs(x[3])))
    return qualifying[0][0], qualifying[0][1], qualifying[0][2]


def r3_resistance(c, h, l, v, tp, sig_idx, adr):
    n, kind, ma_val = lso_period(c, sig_idx, adr)
    if n is None:
        # No MA in range -> fall back to short window
        n = 21
        kind = "fallback21"
    window_start = max(0, sig_idx - n)
    if window_start >= sig_idx: return None, float("nan"), n, kind
    cw = c[window_start:sig_idx]
    A = window_start + int(np.argmax(cw))
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return A, float("nan"), n, kind
    av = float((tp[A:sig_idx+1] * seg_v).sum() / sv)
    return A, av, n, kind


def evaluate_t1(c, sig_idx, avwap_val):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx: return False
    fwd = c[sig_idx+1:end+1]
    return bool(((fwd > sig_close) & (fwd > avwap_val)).any())


total = 0
t1_pass = 0
t1_fail = 0
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
    A, avw, n, kind = r3_resistance(c, h, l, v, tp, sig_idx, adr)
    if A is None or not np.isfinite(avw):
        continue
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    max_fwd = float(c[sig_idx+1:end+1].max()) if end > sig_idx else float("nan")
    passes = evaluate_t1(c, sig_idx, avw)
    if passes: t1_pass += 1
    else: t1_fail += 1
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "A": A, "n_lso": n, "ma_kind": kind,
        "anchor_date": dates[A], "sig_close": sig_close, "avwap": avw,
        "passes": passes, "max_fwd": max_fwd, "dates_arr": dates,
    })

print(f"=== T1.1 (R3 with restricted MA set: SMA50/100/200, EMA3/8/21) ===")
print(f"Total: {total}, Pass: {t1_pass}, Fail: {t1_fail}")
print()

print("=== ANCHOR vs DAN PICK ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'lso':>12} {'bars_off':>9} {'dan_avw':>8} {'rule_avw':>9} {'ratio':>6}")
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
    lso = f"{r['ma_kind']}{r['n_lso']}"
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} {lso:>12} "
          f"{bars_off:>+9d} {dan_avwap_val:>8.2f} {r['avwap']:>9.2f} {ratio:>6.3f}")
if bars_offs:
    print(f"\n  >> mean abs bars off = {np.mean(bars_offs):.1f}")
    print(f"  >> median AVWAP ratio = {np.median(ratios):.3f}")
    print(f"  >> exact bar matches = {sum(1 for b in bars_offs if b == 0)} / {len(bars_offs)}")
    print(f"  >> within 5 bars = {sum(1 for b in bars_offs if b <= 5)} / {len(bars_offs)}")

print()
print("=== LSO MA per setup (sanity: HTF=fast, BF=mid, BASE=long?) ===")
by_setup = {"htf": [], "bf": [], "base": []}
for r in records:
    by_setup[r["setup"]].append(f"{r['ma_kind']}{r['n_lso']}")
for setup in ["htf", "bf", "base"]:
    items = by_setup[setup]
    n = len(items)
    print(f"\n  {setup} (n={n}):")
    for ma, cnt in Counter(items).most_common(8):
        print(f"    {ma:<14} {cnt:>3} ({cnt/n*100:.1f}%)")
