"""H7: Full ruleset T1.1 test on all 113 examples.

Ruleset components:
  Resistance: R3 — highest close in [sig - N_lso, sig - 1] window. AVWAP from there.
  Support: auto-detected — closest MA to sig from below in ADR units (any of {SMA10/21/50/200, EMA10/21/50/200}).
  Foothold: (sig_close - support) / ADR <= T_foothold.
  Entry: exists k in [1,10] with close[sig+k] > close[sig] AND close[sig+k] > resistance_AVWAP.

Sweep T_foothold to find smallest threshold where 113/113 pass.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"

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


def lso_period(c, sig_idx, adr, t_lso=1.5):
    sig_close = c[sig_idx]
    candidates = []
    for n in [10, 21, 50, 200]:
        if sig_idx < n - 1: continue
        ma = float(np.mean(c[sig_idx-n+1:sig_idx+1]))
        signed = (sig_close - ma) / adr
        if abs(signed) <= t_lso:
            candidates.append((n, ma))
    if not candidates:
        return min(10, max(2, sig_idx))
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][0]


def r3_resistance(c, h, l, v, tp, sig_idx, adr):
    n = lso_period(c, sig_idx, adr)
    window_start = max(0, sig_idx - n)
    if window_start >= sig_idx: return None, float("nan")
    cw = c[window_start:sig_idx]
    A = window_start + int(np.argmax(cw))
    seg_v = v[A:sig_idx+1]
    sv = float(seg_v.sum())
    if sv <= 0: return A, float("nan")
    av = float((tp[A:sig_idx+1] * seg_v).sum() / sv)
    return A, av


def detect_support(c, h, l, sig_idx, adr):
    """Closest MA to sig_close from below (positive signed_adr); returns (ma_name, ma_val, signed_adr)."""
    sig_close = c[sig_idx]
    best = None
    candidates = []
    for n in [10, 21, 50, 200]:
        for kind in ["SMA", "EMA"]:
            if sig_idx < n - 1: continue
            ma = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
            if not np.isfinite(ma): continue
            signed = (sig_close - ma) / adr  # positive => below
            candidates.append((f"{kind}{n}", ma, signed))
    if not candidates: return None, float("nan"), float("nan")
    # Prefer below or at sig (signed >= 0); pick smallest signed (closest to sig)
    below = [c for c in candidates if c[2] >= 0]
    if below:
        below.sort(key=lambda x: x[2])
        return below[0]
    # Otherwise pick closest by abs distance
    candidates.sort(key=lambda x: abs(x[2]))
    return candidates[0]


def evaluate_entry(c, sig_idx, avwap_val):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx: return False, float("nan")
    fwd = c[sig_idx+1:end+1]
    valid_k = (fwd > sig_close) & (fwd > avwap_val)
    return bool(valid_k.any()), float(fwd.max())


# Phase 1: collect rule outputs per example
records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0: continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 14: continue
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0: continue

    A, avwap_val = r3_resistance(c, h, l, v, tp, sig_idx, adr)
    sup_name, sup_val, sup_signed = detect_support(c, h, l, sig_idx, adr)
    entry_ok, max_fwd = evaluate_entry(c, sig_idx, avwap_val)

    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": c[sig_idx], "adr": adr,
        "anchor_A": A, "avwap": avwap_val,
        "sup_name": sup_name, "sup_val": sup_val, "sup_signed": sup_signed,
        "entry_ok": entry_ok, "max_fwd": max_fwd,
    })

print(f"Total examples: {len(records)}")

# Phase 2: T1.1 pass at varying T_foothold thresholds
print()
print("=== T1.1 PASS (resistance + foothold + entry) at varying T_foothold ===")
print(f"{'T_foothold':>10} | {'pass_total':>10} | {'pass_htf':>10} | {'pass_bf':>10} | {'pass_base':>10} | {'fails':>50}")
totals = {s: sum(1 for r in records if r["setup"] == s) for s in ["htf", "bf", "base"]}
print(f"{'totals':>10} |             | {totals['htf']:>10} | {totals['bf']:>10} | {totals['base']:>10}")

for t_foothold in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0]:
    pass_count = {s: 0 for s in ["htf", "bf", "base"]}
    fails = []
    for r in records:
        # Validity at sig: support close enough (foothold)
        sup_ok = r["sup_signed"] is not None and np.isfinite(r["sup_signed"]) and r["sup_signed"] <= t_foothold
        # Entry valid: r["entry_ok"]
        if sup_ok and r["entry_ok"]:
            pass_count[r["setup"]] += 1
        else:
            fails.append((r["setup"], r["ticker"], r["entry_date"], "sup_no" if not sup_ok else "entry_no",
                          r["sup_name"], f"{r['sup_signed']:.2f}A", f"{r['avwap']:.2f}", f"{r['max_fwd']:.2f}"))
    total_pass = sum(pass_count.values())
    fail_summary = ""
    if fails and total_pass < len(records):
        sample = fails[:3]
        fail_summary = "; ".join(f"{f[1]}-{f[3]}({f[5]})" for f in sample)
    print(f"{t_foothold:>10.2f} | {total_pass:>10} | {pass_count['htf']:>10} | {pass_count['bf']:>10} | {pass_count['base']:>10} | {fail_summary}")

# Phase 3: at smallest T where all pass, list rule output stats
print()
print("=== Detailed T_foothold analysis: minimum T where ALL examples pass ===")
sup_signed_arr = np.array([r["sup_signed"] for r in records if np.isfinite(r["sup_signed"])])
print(f"sup_signed distribution across {len(sup_signed_arr)} records:")
print(f"  min={sup_signed_arr.min():.2f}, p25={np.percentile(sup_signed_arr,25):.2f}, "
      f"median={np.percentile(sup_signed_arr,50):.2f}, p75={np.percentile(sup_signed_arr,75):.2f}, "
      f"p90={np.percentile(sup_signed_arr,90):.2f}, max={sup_signed_arr.max():.2f}")

# Min T to pass all = max sup_signed among records that have entry_ok
entry_ok_records = [r for r in records if r["entry_ok"]]
print(f"\nRecords with entry_ok=True: {len(entry_ok_records)} / {len(records)}")
entry_failures = [r for r in records if not r["entry_ok"]]
if entry_failures:
    print(f"\nRecords where entry_ok=False (R3 + AND-gate fails — UNPATCHABLE by foothold):")
    for r in entry_failures:
        print(f"  {r['setup']:<6} {r['ticker']:<6} {r['entry_date']}  avwap={r['avwap']:.2f} max_fwd={r['max_fwd']:.2f}")

if entry_ok_records:
    max_signed_among_pass = max(r["sup_signed"] for r in entry_ok_records if np.isfinite(r["sup_signed"]))
    print(f"\nMin T_foothold to pass all entry_ok records = {max_signed_among_pass:.3f} ADR")
    print(f"That's the support distance for the worst-foothold valid example.")
    # Which record is the worst foothold?
    worst = max(entry_ok_records, key=lambda r: r["sup_signed"] if np.isfinite(r["sup_signed"]) else -np.inf)
    print(f"Worst foothold record: {worst['setup']} {worst['ticker']} {worst['entry_date']}: "
          f"sup={worst['sup_name']} signed={worst['sup_signed']:.2f}A")
