"""H17: fixed 200-bar lookback for resistance AVWAP regardless of support MA period.

Rule:
- Support MA = longest-period MA in {EMA3,8,21, SMA50,100,200} with
  |sig_low - MA| / ADR <= FOOTHOLD_CAP (0.50 ADR).
- Lookback window = fixed 200 bars: [sig - 200, sig - 1].
- Resistance AVWAP = argmax AVWAP in lookback window.
- Corridor width = (resistance AVWAP - support MA) / ADR.

Verifies Dan's constraint: for every valid signal, AVWAP > sig_close.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 100), ("SMA", 200)]
FOOTHOLD_CAP = 0.50
LOOKBACK = 200

DAN_PICKS = {
    ("AR",   "2020-12-17"): ("2020-12-11", 5.01),
    ("BB",   "2024-12-20"): ("2024-12-17", 3.07),
    ("DRN",  "2024-07-11"): ("2023-12-08", 8.54),
    ("LMND", "2024-11-05"): ("2024-11-01", 24.50),
    ("PTON", "2024-10-11"): ("2024-09-20", 4.77),
    ("REAL", "2024-11-13"): ("2024-11-06", 3.79),
    ("QUBT", "2024-11-20"): ("2024-11-14", None),
}

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples "
                        "WHERE setup_type IN ('htf','bf','base') "
                        "ORDER BY setup_type, ticker, entry_date").fetchall()


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy(); df["date"] = pd.to_datetime(df["date"])
    d = df["date"].dt.strftime("%Y-%m-%d").values
    return (d, df["high"].values.astype(float), df["low"].values.astype(float),
            df["close"].values.astype(float), df["volume"].values.astype(float))


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")
def sma(a, n, i): return float(np.mean(a[i-n+1:i+1])) if i >= n-1 else float("nan")
def ema(a, n, i):
    if i < n-1: return float("nan")
    alpha = 2.0/(n+1); e = a[0]
    for k in range(1, i+1): e = alpha*a[k] + (1-alpha)*e
    return float(e)


def pick_support(c, l, i, adr, cap):
    sl = l[i]; qual = []
    for kind, n in MA_SET:
        if i < n-1: continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        if not np.isfinite(v): continue
        fh = abs(sl - v) / adr
        if fh <= cap:
            qual.append((n, kind, v, fh))
    if not qual: return None
    qual.sort(key=lambda x: -x[0])  # longest period first
    return qual[0]  # (period, kind, val, foothold)


def argmax_avwap(h, l, c, v, i, n_win):
    tp = (h + l + c) / 3.0
    A_start = max(0, i - n_win); A_end = i
    if A_start >= A_end: return None
    ctpv = np.concatenate([[0.0], np.cumsum(tp*v)])
    cv = np.concatenate([[0.0], np.cumsum(v)])
    Ar = np.arange(A_start, A_end)
    tpv = ctpv[i+1] - ctpv[Ar]; vv = cv[i+1] - cv[Ar]
    with np.errstate(invalid="ignore", divide="ignore"):
        av = np.where(vv > 0, tpv/vv, -np.inf)
    bi = int(np.argmax(av))
    return int(Ar[bi]), float(av[bi])


def t1_passes(c, i, avwap_val):
    end = min(i + 10, len(c) - 1)
    if end <= i: return False
    fwd = c[i+1:end+1]
    return bool(((fwd > c[i]) & (fwd > avwap_val)).any())


records = []
rejected = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c, v = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue
    sup = pick_support(c, l, i, adr, FOOTHOLD_CAP)
    if sup is None:
        rejected.append((setup, ticker, entry_date, "no_MA_within_cap"))
        continue
    period, kind, val, foothold = sup
    r = argmax_avwap(h, l, c, v, i, LOOKBACK)
    if r is None:
        rejected.append((setup, ticker, entry_date, "no_avwap"))
        continue
    A, avwap = r
    corridor = (avwap - val) / adr
    records.append({
        "setup": setup, "ticker": ticker, "entry": entry_date,
        "sig_idx": i, "sig_close": c[i], "sig_low": l[i], "adr": adr,
        "support_kind": kind, "support_n": period, "support_val": val,
        "foothold": foothold, "support_dist": (c[i]-val)/adr,
        "anchor_idx": A, "anchor_date": d[A], "avwap": avwap,
        "resist_dist": (avwap - c[i])/adr,
        "corridor": corridor,
        "resist_above_sig": avwap > c[i],
        "t1_pass": t1_passes(c, i, avwap),
        "dates": d,
    })

print(f"Kept: {len(records)}  Rejected: {len(rejected)}")
for s, t, e, r in rejected:
    print(f"  REJECTED {t} {e} ({s}): {r}")
print()

# Global constraint check
below_sig = [r for r in records if not r["resist_above_sig"]]
print(f"=== DAN'S HARD CONSTRAINT: sig_close < AVWAP ===")
print(f"  Above sig_close: {len(records) - len(below_sig)}/{len(records)}")
if below_sig:
    print(f"  VIOLATIONS (should be zero):")
    for r in below_sig:
        print(f"    {r['ticker']} {r['entry']} ({r['setup']}): "
              f"sig_close={r['sig_close']:.3f} avwap={r['avwap']:.3f} "
              f"resist_dist={r['resist_dist']:+.3f} ADR")
print()

# T1.1 check
t1_count = sum(1 for r in records if r["t1_pass"])
print(f"=== T1.1 (entry close breaks sig_close AND AVWAP) ===")
print(f"  Pass: {t1_count}/{len(records)}")
t1_fails = [r for r in records if not r["t1_pass"]]
for r in t1_fails:
    print(f"    FAIL {r['ticker']} {r['entry']} ({r['setup']}): "
          f"sig_close={r['sig_close']:.3f} avwap={r['avwap']:.3f}")
print()

# Per-setup corridor distributions
print("=== PER-SETUP CORRIDOR WIDTH (fixed 200-bar lookback) ===")
for setup in ("htf", "bf", "base"):
    sub = [r for r in records if r["setup"] == setup]
    if not sub: continue
    w = np.array([r["corridor"] for r in sub])
    print(f"  {setup:<5} n={len(sub):>3}  min={np.min(w):+.2f}  p25={np.percentile(w,25):.2f}  "
          f"p50={np.percentile(w,50):.2f}  p75={np.percentile(w,75):.2f}  "
          f"p90={np.percentile(w,90):.2f}  max={np.max(w):.2f}")
    mas = Counter(f"{r['support_kind']}{r['support_n']}" for r in sub)
    print(f"        MA pick: " + "  ".join(f"{ma}:{cnt}" for ma, cnt in mas.most_common()))
print()

# Widest 10 examples
print("=== WIDEST 10 EXAMPLES ===")
print(f"{'ticker':<6} {'entry':<12} {'setup':<5} {'support':>8} {'anchor':<12} "
      f"{'anchor_bars':>11} {'res_dist':>9} {'corridor':>9}")
top10 = sorted(records, key=lambda r: -r["corridor"])[:10]
for r in top10:
    bars_back = r["sig_idx"] - r["anchor_idx"]
    supma = f"{r['support_kind']}{r['support_n']}"
    print(f"{r['ticker']:<6} {r['entry']:<12} {r['setup']:<5} {supma:>8} "
          f"{r['anchor_date']:<12} {bars_back:>11d} {r['resist_dist']:>+9.2f} "
          f"{r['corridor']:>+9.2f}")
print()

# Tightest 10 (check whether the artifact problem is fixed)
print("=== TIGHTEST 10 EXAMPLES ===")
print(f"{'ticker':<6} {'entry':<12} {'setup':<5} {'support':>8} {'anchor':<12} "
      f"{'anchor_bars':>11} {'res_dist':>9} {'corridor':>9}")
bot10 = sorted(records, key=lambda r: r["corridor"])[:10]
for r in bot10:
    bars_back = r["sig_idx"] - r["anchor_idx"]
    supma = f"{r['support_kind']}{r['support_n']}"
    print(f"{r['ticker']:<6} {r['entry']:<12} {r['setup']:<5} {supma:>8} "
          f"{r['anchor_date']:<12} {bars_back:>11d} {r['resist_dist']:>+9.2f} "
          f"{r['corridor']:>+9.2f}")
print()

# DRN + QUBT detailed
print("=== DRN + QUBT DETAILED ===")
for tkr in ("DRN", "QUBT"):
    for r in records:
        if r["ticker"] != tkr: continue
        bars_back = r["sig_idx"] - r["anchor_idx"]
        print(f"  {tkr} {r['entry']} ({r['setup']})")
        print(f"    sig_close={r['sig_close']:.3f}  ADR={r['adr']:.3f}")
        print(f"    support  = {r['support_kind']}{r['support_n']} @ {r['support_val']:.3f}  "
              f"foothold={r['foothold']:.2f}  dist={r['support_dist']:+.2f} ADR")
        print(f"    anchor   = {r['anchor_date']} ({bars_back} bars back)  AVWAP={r['avwap']:.3f}  "
              f"res_dist={r['resist_dist']:+.2f} ADR")
        print(f"    corridor = {r['corridor']:+.3f} ADR")
        print(f"    T1.1     = {r['t1_pass']}")
print()

# Dan hand-pick anchor match
print("=== DAN HAND-PICK ANCHOR MATCH ===")
print(f"{'ticker':<6} {'entry':<12} {'dan_anchor':<12} {'rule_anchor':<12} {'bars_off':>9} "
      f"{'support':>8} {'rule_avwap':>10}")
for r in records:
    key = (r["ticker"], r["entry"])
    if key not in DAN_PICKS: continue
    dan_anchor_date, _ = DAN_PICKS[key]
    hit = np.where(r["dates"] == dan_anchor_date)[0]
    if len(hit) == 0: continue
    dan_A = int(hit[0])
    bars_off = r["anchor_idx"] - dan_A
    supma = f"{r['support_kind']}{r['support_n']}"
    print(f"{r['ticker']:<6} {r['entry']:<12} {dan_anchor_date:<12} {r['anchor_date']:<12} "
          f"{bars_off:>+9d} {supma:>8} {r['avwap']:>10.3f}")
