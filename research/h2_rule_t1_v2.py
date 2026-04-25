"""H2 iteration v2: same 5 rules, but evaluate against:
  - All 113 examples (lower sig_idx requirement)
  - Dan's 8 picks (anchor/AVWAP comparison)
  - Wild signal pool (filter strength)

Output:
  - T1.1 pass rate per rule
  - Anchor/AVWAP diff vs Dan picks per rule
  - Wild gate-pass rate per rule (lower = stronger filter)
"""
import json
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
POOL_DIR = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier/research/classifier_pool"

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


def sma_series(arr, n):
    out = np.full_like(arr, np.nan, dtype=float)
    for i in range(n-1, len(arr)):
        out[i] = float(np.mean(arr[i-n+1:i+1]))
    return out


def adr14_at(h, l, idx):
    if idx < 14: return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def lso_period(c, sig_idx, adr, t=1.5):
    sig_close = c[sig_idx]
    candidates = []
    # Try smaller MAs first; only use SMA200 if sig_idx >= 199
    for n in [10, 21, 50, 200]:
        if sig_idx < n - 1: continue
        ma = float(np.mean(c[sig_idx-n+1:sig_idx+1]))
        signed = (sig_close - ma) / adr
        if abs(signed) <= t:
            candidates.append((n, ma))
    if not candidates:
        return min(10, sig_idx)
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][0]


def avwap(tp, v, A, sig_idx):
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return float("nan")
    return float((tp[A:sig_idx+1] * seg_v).sum() / sv)


def rule_r1(c, h, l, v, tp, sig_idx, adr):
    n = lso_period(c, sig_idx, adr)
    A = max(0, sig_idx - n)
    return A, avwap(tp, v, A, sig_idx), n


def rule_r2(c, h, l, v, tp, sig_idx, adr):
    n = lso_period(c, sig_idx, adr)
    sma = sma_series(c, n)
    lo_window_start = max(n - 1, sig_idx - 2*n)
    if lo_window_start >= sig_idx: return None, float("nan"), n
    sma_window = sma[lo_window_start:sig_idx]
    valid = ~np.isnan(sma_window)
    if not valid.any(): return None, float("nan"), n
    A = lo_window_start + int(np.argmin(np.where(valid, sma_window, np.inf)))
    return A, avwap(tp, v, A, sig_idx), n


def rule_r3(c, h, l, v, tp, sig_idx, adr):
    n = lso_period(c, sig_idx, adr)
    window_start = max(0, sig_idx - n)
    if window_start >= sig_idx: return None, float("nan"), n
    cw = c[window_start:sig_idx]
    A = window_start + int(np.argmax(cw))
    return A, avwap(tp, v, A, sig_idx), n


def rule_r4(c, h, l, v, tp, sig_idx, adr):
    n = lso_period(c, sig_idx, adr)
    sma = sma_series(c, n)
    A = None
    for i in range(sig_idx - 1, max(n - 1, 0), -1):
        if not np.isfinite(sma[i]): continue
        if c[i] < sma[i]:
            A = i
            break
    if A is None:
        A = max(0, sig_idx - n)
    return A, avwap(tp, v, A, sig_idx), n


def rule_r5(c, h, l, v, tp, sig_idx, adr):
    A = max(0, sig_idx - 21)
    return A, avwap(tp, v, A, sig_idx), 21


RULES = [
    ("R1_anchor_sig_minus_N", rule_r1),
    ("R2_sma_bottom",         rule_r2),
    ("R3_highest_close_in_N", rule_r3),
    ("R4_last_below_sma",     rule_r4),
    ("R5_fixed_21",           rule_r5),
]


def evaluate_t1(c, sig_idx, avwap_val):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx: return False
    fwd = c[sig_idx+1:end+1]
    return bool(((fwd > sig_close) & (fwd > avwap_val)).any())


# Phase 1: T1.1 on examples
print("=== T1.1 PASS RATE (113 examples) ===")
ex_results = {name: {"pass": 0, "fail": 0, "fails": []} for name, _ in RULES}
ex_records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 50: continue
    adr = adr14_at(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: continue
    rec = {"setup": setup, "ticker": ticker, "entry_date": entry_date, "sig_idx": sig_idx,
           "sig_close": c[sig_idx], "adr": adr, "dates_arr": dates,
           "h": h, "l": l, "c": c, "v": v, "tp": tp}
    ex_records.append(rec)
    for rname, rfn in RULES:
        A, av, n = rfn(c, h, l, v, tp, sig_idx, adr)
        if not np.isfinite(av):
            ex_results[rname]["fail"] += 1
            ex_results[rname]["fails"].append((setup, ticker, entry_date, "nan-AVWAP"))
            continue
        if evaluate_t1(c, sig_idx, av):
            ex_results[rname]["pass"] += 1
        else:
            ex_results[rname]["fail"] += 1
            sig_close = c[sig_idx]
            max_fwd = float(c[sig_idx+1:min(sig_idx+11, len(c))].max())
            ex_results[rname]["fails"].append((setup, ticker, entry_date,
                f"sig={sig_close:.2f} avw={av:.2f} max_fwd={max_fwd:.2f} N={n}"))

for rname, _ in RULES:
    r = ex_results[rname]
    print(f"  {rname:<30} {r['pass']}/{r['pass']+r['fail']} pass")

print()
print("=== T1.1 FAILURES (per rule) ===")
for rname, _ in RULES:
    if not ex_results[rname]["fails"]: continue
    print(f"\n  {rname}:")
    for s, t, e, info in ex_results[rname]["fails"]:
        print(f"    {s:<6} {t:<8} {e}  {info}")

# Phase 2: Anchor/AVWAP comparison vs Dan picks on 8 failures
print()
print("=== ANCHOR vs DAN PICK on 8 failures (bars off, AVWAP ratio) ===")
for rname, rfn in RULES:
    print(f"\n  {rname}:")
    bars_offs = []
    avwap_ratios = []
    for rec in ex_records:
        key = (rec["ticker"], rec["entry_date"])
        if key not in DAN_PICKS: continue
        dan_anchor_date, dan_avwap_val = DAN_PICKS[key]
        dan_a_idx = np.where(rec["dates_arr"] == dan_anchor_date)[0]
        if len(dan_a_idx) == 0: continue
        dan_A = int(dan_a_idx[0])
        A, av, n = rfn(rec["c"], rec["h"], rec["l"], rec["v"], rec["tp"], rec["sig_idx"], rec["adr"])
        if A is None: continue
        bars_off = abs(A - dan_A)
        avwap_ratio = av / dan_avwap_val if dan_avwap_val > 0 else float("nan")
        bars_offs.append(bars_off)
        avwap_ratios.append(avwap_ratio)
        print(f"    {rec['ticker']:<6} {rec['entry_date']:<12} dan_A={dan_anchor_date} ({rec['sig_idx']-dan_A}b back) "
              f"rule_A={rec['dates_arr'][A]} ({rec['sig_idx']-A}b back) bars_off={A-dan_A:+d} "
              f"AVWAP rule={av:.2f} dan={dan_avwap_val:.2f} ratio={avwap_ratio:.3f}")
    if bars_offs:
        print(f"    >> mean abs bars off = {np.mean(bars_offs):.1f}, median AVWAP ratio = {np.median(avwap_ratios):.3f}")

# Phase 3: Wild filter strength
print()
print("=== WILD FILTER (per rule, on classifier_pool/{setup}_pool.json) ===")
for setup_name in ["htf", "bf", "base"]:
    pool_file = f"{POOL_DIR}/{setup_name}_pool.json"
    try:
        with open(pool_file) as f:
            pool = json.load(f)
    except Exception as e:
        print(f"  {setup_name}: load error {e}")
        continue
    # Pool format: list of clusters with bar_idx info; need to read schema
    # Will adapt based on what's there
    if not pool: continue
    # peek schema
    sample = pool[0] if isinstance(pool, list) else next(iter(pool.values()), None)
    print(f"  {setup_name}: pool size = {len(pool)}, sample keys = {list(sample.keys())[:8] if isinstance(sample, dict) else 'non-dict'}")
