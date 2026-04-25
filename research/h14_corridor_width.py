"""H14: corridor width (resistance - support) / ADR at signal bar.

Goal: with OHLCV cache rebuilt on raw close (distribution-adjustment bug fixed
2026-04-23), re-measure the vertical spread between the locked resistance
(argmax AVWAP in MA-period window) and the locked support (auto-detected
longest MA within T_LSO=2.0 ADR) across all labeled examples. Old warped-cache
spread landed at ~2 ADR max, which was too loose to discriminate wild signals.

Reuses h11_argmax_avwap_in_window.py mechanics verbatim for the rule; adds
corridor-width distribution analysis and a DRN-specific detailed row.
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
    ("LMND", "2024-11-05"): ("2024-11-01", 24.50),
    ("PTON", "2024-10-11"): ("2024-09-20", 4.77),
    ("REAL", "2024-11-13"): ("2024-11-06", 3.79),
}

MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 100), ("SMA", 200)]
T_LSO = 2.0

# --- cache sanity ---
with open(CACHE, "rb") as f:
    universe = pickle.load(f)
print(f"OHLCV cache: {CACHE}")
print(f"Ticker count: {len(universe)}")
if len(universe) < 11200:
    raise SystemExit(f"ABORT: ticker count {len(universe)} < 11200 — cache looks wrong")

with sqlite3.connect(DB) as conn:
    rows = conn.execute(
        "SELECT setup_type, ticker, entry_date FROM examples "
        "WHERE setup_type IN ('htf','bf','base') ORDER BY setup_type, ticker, entry_date"
    ).fetchall()
print(f"DB examples (htf/bf/base): {len(rows)}")
print()


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
    if idx < 14:
        return float("nan")
    return float(np.mean(h[idx-13:idx+1] - l[idx-13:idx+1]))


def sma_at(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    return float(np.mean(arr[idx-n+1:idx+1]))


def ema_at(arr, n, idx):
    if idx < n - 1:
        return float("nan")
    alpha = 2.0 / (n + 1)
    e = arr[0]
    for i in range(1, idx + 1):
        e = alpha * arr[i] + (1 - alpha) * e
    return float(e)


def lso_support(c, sig_idx, adr, t_lso=T_LSO):
    sig_close = c[sig_idx]
    qualifying = []
    for kind, n in MA_SET:
        if sig_idx < n - 1:
            continue
        val = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
        if not np.isfinite(val):
            continue
        signed = (sig_close - val) / adr
        if abs(signed) <= t_lso:
            qualifying.append((n, kind, val, signed))
    if not qualifying:
        return None
    qualifying.sort(key=lambda x: (-x[0], abs(x[3])))
    n, kind, val, signed = qualifying[0]
    return {"n": n, "kind": kind, "value": val, "signed_adr": signed}


def avwap_curve(tp, v, sig_idx, n_window):
    A_start = max(0, sig_idx - n_window)
    A_end = sig_idx
    if A_start >= A_end:
        return None, None
    cum_tpv = np.concatenate([[0.0], np.cumsum(tp * v)])
    cum_v = np.concatenate([[0.0], np.cumsum(v)])
    A_range = np.arange(A_start, A_end)
    total_tpv = cum_tpv[sig_idx + 1] - cum_tpv[A_range]
    total_v = cum_v[sig_idx + 1] - cum_v[A_range]
    with np.errstate(invalid="ignore", divide="ignore"):
        avwaps = np.where(total_v > 0, total_tpv / total_v, -np.inf)
    return A_range, avwaps


def r3_resistance(tp, v, sig_idx, n_lso):
    A_range, avwaps = avwap_curve(tp, v, sig_idx, n_lso)
    if A_range is None:
        return None
    best_local = int(np.argmax(avwaps))
    return {"A": int(A_range[best_local]), "avwap": float(avwaps[best_local])}


def t1_passes(c, sig_idx, avwap_val):
    sig_close = c[sig_idx]
    end = min(sig_idx + 10, len(c) - 1)
    if end <= sig_idx:
        return False
    fwd = c[sig_idx+1:end+1]
    return bool(((fwd > sig_close) & (fwd > avwap_val)).any())


records = []
missing = 0
for setup, ticker, entry_date in rows:
    if ticker not in universe:
        missing += 1
        continue
    dates, h, l, c, v, tp = load(ticker)
    hits = np.where(dates == entry_date)[0]
    if len(hits) == 0:
        missing += 1
        continue
    entry_idx = int(hits[0])
    sig_idx = entry_idx - 1
    if sig_idx < 14:
        continue
    adr = adr14(h, l, sig_idx)
    if not np.isfinite(adr) or adr <= 0:
        continue
    sup = lso_support(c, sig_idx, adr)
    if sup is None:
        continue
    res = r3_resistance(tp, v, sig_idx, sup["n"])
    if res is None:
        continue
    sig_close = float(c[sig_idx])
    support_dist_adr = (sig_close - sup["value"]) / adr      # positive = support below close
    resistance_dist_adr = (res["avwap"] - sig_close) / adr   # positive = resistance above close
    corridor_width = (res["avwap"] - sup["value"]) / adr     # total width in ADR
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "adr": adr,
        "ma_kind": sup["kind"], "n_lso": sup["n"], "support_val": sup["value"],
        "support_dist_adr": support_dist_adr,
        "A": res["A"], "anchor_date": dates[res["A"]], "avwap": res["avwap"],
        "resistance_dist_adr": resistance_dist_adr,
        "corridor_width": corridor_width,
        "t1_pass": t1_passes(c, sig_idx, res["avwap"]),
        "dates_arr": dates,
    })

print(f"Evaluable examples: {len(records)} (missing: {missing})")
t1_pass_count = sum(1 for r in records if r["t1_pass"])
print(f"T1.1 pass: {t1_pass_count}/{len(records)}")
print()


# --- corridor width distribution ---
widths = np.array([r["corridor_width"] for r in records])
print("=== CORRIDOR WIDTH (resistance_AVWAP - support_MA) / ADR ===")
print(f"  overall n={len(widths)}")
print(f"  min    = {np.min(widths):.3f}")
print(f"  p10    = {np.percentile(widths, 10):.3f}")
print(f"  p25    = {np.percentile(widths, 25):.3f}")
print(f"  p50    = {np.percentile(widths, 50):.3f}")
print(f"  p75    = {np.percentile(widths, 75):.3f}")
print(f"  p90    = {np.percentile(widths, 90):.3f}")
print(f"  p95    = {np.percentile(widths, 95):.3f}")
print(f"  max    = {np.max(widths):.3f}")
print(f"  mean   = {np.mean(widths):.3f}")
print()

print("=== PER-SETUP WIDTH ===")
for setup in ("htf", "bf", "base"):
    sub = np.array([r["corridor_width"] for r in records if r["setup"] == setup])
    if len(sub) == 0:
        continue
    print(f"  {setup:<5} n={len(sub):>3}  min={np.min(sub):.2f}  p50={np.percentile(sub,50):.2f}  "
          f"p90={np.percentile(sub,90):.2f}  max={np.max(sub):.2f}")
print()

# --- top-10 widest ---
print("=== WIDEST 10 EXAMPLES (ceiling setters) ===")
print(f"{'ticker':<6} {'entry':<12} {'setup':<5} {'support':>10} {'resist':>8} {'width_adr':>9}")
top10 = sorted(records, key=lambda r: -r["corridor_width"])[:10]
for r in top10:
    supma = f"{r['ma_kind']}{r['n_lso']}"
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {r['setup']:<5} {supma:>10} "
          f"{r['resistance_dist_adr']:>+8.2f} {r['corridor_width']:>9.3f}")
print()

# --- DRN detailed row ---
print("=== DRN DETAILED (bug-discovery ticker) ===")
drn = [r for r in records if r["ticker"] == "DRN"]
for r in drn:
    dan_pick = DAN_PICKS.get((r["ticker"], r["entry_date"]))
    print(f"  DRN {r['entry_date']}  setup={r['setup']}")
    print(f"    sig_close        = {r['sig_close']:.4f}")
    print(f"    ADR(14)          = {r['adr']:.4f}")
    print(f"    support MA       = {r['ma_kind']}{r['n_lso']} @ {r['support_val']:.4f}")
    print(f"    support dist     = {r['support_dist_adr']:+.3f} ADR  (sig - support)")
    print(f"    resistance anchor= {r['anchor_date']}")
    print(f"    resistance AVWAP = {r['avwap']:.4f}")
    print(f"    resistance dist  = {r['resistance_dist_adr']:+.3f} ADR  (resist - sig)")
    print(f"    corridor width   = {r['corridor_width']:.3f} ADR")
    print(f"    T1.1 pass        = {r['t1_pass']}")
    if dan_pick:
        dan_anchor_date, dan_avwap_val = dan_pick
        print(f"    dan_anchor_date  = {dan_anchor_date}  (rule picked: {r['anchor_date']})")
        print(f"    dan_anchor_avwap = {dan_avwap_val:.4f}  (rule avwap: {r['avwap']:.4f}, "
              f"ratio {r['avwap']/dan_avwap_val:.3f})")
print()

# --- Dan hand-pick anchor match sanity ---
print("=== DAN HAND-PICK ANCHOR COMPARISON ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'bars_off':>9} "
      f"{'dan_avw':>8} {'rule_avw':>9} {'ratio':>6}")
bars_offs, ratios = [], []
for r in records:
    key = (r["ticker"], r["entry_date"])
    if key not in DAN_PICKS:
        continue
    dan_anchor_date, dan_avwap_val = DAN_PICKS[key]
    dan_a_hits = np.where(r["dates_arr"] == dan_anchor_date)[0]
    if len(dan_a_hits) == 0:
        continue
    dan_A = int(dan_a_hits[0])
    bars_off = r["A"] - dan_A
    ratio = r["avwap"] / dan_avwap_val if dan_avwap_val > 0 else float("nan")
    bars_offs.append(abs(bars_off))
    ratios.append(ratio)
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} "
          f"{bars_off:>+9d} {dan_avwap_val:>8.2f} {r['avwap']:>9.2f} {ratio:>6.3f}")
if bars_offs:
    print(f"  mean abs bars off   = {np.mean(bars_offs):.1f}")
    print(f"  median AVWAP ratio  = {np.median(ratios):.3f}")
    print(f"  exact bar matches   = {sum(1 for b in bars_offs if b == 0)} / {len(bars_offs)}")
    print(f"  within 5 bars       = {sum(1 for b in bars_offs if b <= 5)} / {len(bars_offs)}")
