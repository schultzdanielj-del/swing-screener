"""H15: corridor width with support = "MA the lows respect" selection rule.

Change from h14/h11: support MA selection is the MA with the smallest
|sig_low - MA| / ADR (the MA whose level the lead-up lows cluster against),
subject to |sig_low - MA| / ADR <= T_FOOTHOLD_CAP = 1.856.

Previous "longest period with |sig_close - MA| / ADR <= 2.0" rule drifted
from Dan's mental model (per project_setup_relevant_mas.md: "Auto-detect by
which MA the lead-up lows respect") and produced EMA21 for QUBT where the
actual respected support is EMA8. See h14_qubt_diag.py output.

Restricted MA set unchanged: SMA50/100/200, EMA3/8/21.
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
    # QUBT 2024-11-20: dan anchor 2024-11-14, support EMA8 (per 2026-04-23 correction)
    ("QUBT", "2024-11-20"): ("2024-11-14", None),
}

MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 100), ("SMA", 200)]
T_FOOTHOLD_CAP = 1.856  # from original R3 derivation

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
print(f"OHLCV cache: {CACHE}")
print(f"Ticker count: {len(universe)}")
if len(universe) < 11200:
    raise SystemExit(f"ABORT: ticker count {len(universe)} < 11200")

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


def lows_respect_support(c, l, sig_idx, adr, t_foothold=T_FOOTHOLD_CAP):
    """Pick MA with smallest |sig_low - MA| / ADR, subject to <= t_foothold."""
    sig_low = l[sig_idx]
    candidates = []
    for kind, n in MA_SET:
        if sig_idx < n - 1:
            continue
        val = sma_at(c, n, sig_idx) if kind == "SMA" else ema_at(c, n, sig_idx)
        if not np.isfinite(val):
            continue
        foothold = abs(sig_low - val) / adr
        if foothold <= t_foothold:
            candidates.append((foothold, n, kind, val))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    foothold, n, kind, val = candidates[0]
    return {"n": n, "kind": kind, "value": val, "foothold_adr": foothold}


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
    sup = lows_respect_support(c, l, sig_idx, adr)
    if sup is None:
        continue
    res = r3_resistance(tp, v, sig_idx, sup["n"])
    if res is None:
        continue
    sig_close = float(c[sig_idx])
    sig_low = float(l[sig_idx])
    support_dist_adr = (sig_close - sup["value"]) / adr
    resistance_dist_adr = (res["avwap"] - sig_close) / adr
    corridor_width = (res["avwap"] - sup["value"]) / adr
    records.append({
        "setup": setup, "ticker": ticker, "entry_date": entry_date,
        "sig_idx": sig_idx, "sig_close": sig_close, "sig_low": sig_low, "adr": adr,
        "ma_kind": sup["kind"], "n_lso": sup["n"], "support_val": sup["value"],
        "foothold_adr": sup["foothold_adr"],
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


widths = np.array([r["corridor_width"] for r in records])
print("=== CORRIDOR WIDTH (lows-respect selection) ===")
print(f"  n={len(widths)}")
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

print("=== WIDEST 10 EXAMPLES ===")
print(f"{'ticker':<6} {'entry':<12} {'setup':<5} {'support':>10} {'foothold':>9} {'res_dist':>9} {'width':>9}")
top10 = sorted(records, key=lambda r: -r["corridor_width"])[:10]
for r in top10:
    supma = f"{r['ma_kind']}{r['n_lso']}"
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {r['setup']:<5} {supma:>10} "
          f"{r['foothold_adr']:>9.3f} {r['resistance_dist_adr']:>+9.2f} {r['corridor_width']:>9.3f}")
print()

print("=== PER-SETUP MA DISTRIBUTION (sanity vs memory) ===")
by_setup = {"htf": [], "bf": [], "base": []}
for r in records:
    by_setup[r["setup"]].append(f"{r['ma_kind']}{r['n_lso']}")
for setup in ["htf", "bf", "base"]:
    items = by_setup[setup]
    n = len(items)
    print(f"  {setup} (n={n}):")
    for ma, cnt in Counter(items).most_common(8):
        print(f"    {ma:<10} {cnt:>3} ({cnt/n*100:.1f}%)")
print()

print("=== KEY EXAMPLES (QUBT + DRN) ===")
for tkr in ("QUBT", "DRN"):
    rs = [r for r in records if r["ticker"] == tkr]
    for r in rs:
        print(f"  {tkr} {r['entry_date']}  setup={r['setup']}")
        print(f"    sig_close={r['sig_close']:.4f}  sig_low={r['sig_low']:.4f}  ADR={r['adr']:.4f}")
        print(f"    support  = {r['ma_kind']}{r['n_lso']} @ {r['support_val']:.4f}  "
              f"foothold={r['foothold_adr']:.3f} ADR  support_dist={r['support_dist_adr']:+.3f} ADR")
        print(f"    resist   = AVWAP({r['anchor_date']}..sig) = {r['avwap']:.4f}  "
              f"resist_dist={r['resistance_dist_adr']:+.3f} ADR")
        print(f"    width    = {r['corridor_width']:.3f} ADR")
        print(f"    T1.1     = {r['t1_pass']}")
print()

print("=== DAN HAND-PICK ANCHOR + SUPPORT MATCH ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'bars_off':>9} "
      f"{'rule_ma':>8} {'rule_avwap':>10}")
for r in records:
    key = (r["ticker"], r["entry_date"])
    if key not in DAN_PICKS:
        continue
    dan_anchor_date, _dan_avwap_val = DAN_PICKS[key]
    dan_a_hits = np.where(r["dates_arr"] == dan_anchor_date)[0]
    if len(dan_a_hits) == 0:
        continue
    dan_A = int(dan_a_hits[0])
    bars_off = r["A"] - dan_A
    supma = f"{r['ma_kind']}{r['n_lso']}"
    print(f"{r['ticker']:<6} {r['entry_date']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} "
          f"{bars_off:>+9d} {supma:>8} {r['avwap']:>10.4f}")
