"""H19: per-setup bounding box with IQR-based cushion.

Rule (locked 2026-04-23):
- For each example, compute |sig_close - MA| / ADR for each MA in
  {EMA3, EMA8, EMA21, SMA50, SMA200, SMA330}.
- Per setup, per MA: box = [min - cushion, max + cushion] where
  cushion = 0.25 * IQR (p75 - p25) of the examples' distances.
- Missing MAs drop out (don't alter data).
- Raw-distance floor at 0 (can't be below zero physically).

A wild signal is "ready" iff every MA where data exists falls inside
the inflated box.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 200), ("SMA", 330)]
IQR_K = 0.25

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
print(f"OHLCV cache: {CACHE}")
print(f"Ticker count: {len(universe)}")
if len(universe) < 11200:
    raise SystemExit(f"ABORT: ticker count {len(universe)} < 11200")

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
            df["close"].values.astype(float))


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")
def sma(a, n, i): return float(np.mean(a[i-n+1:i+1])) if i >= n-1 else float("nan")
def ema(a, n, i):
    if i < n-1: return float("nan")
    alpha = 2.0/(n+1); e = a[0]
    for k in range(1, i+1): e = alpha*a[k] + (1-alpha)*e
    return float(e)


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue
    sc = c[i]
    dists = {}
    for kind, n in MA_SET:
        if i < n-1:
            dists[f"{kind}{n}"] = float("nan")
            continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        dists[f"{kind}{n}"] = abs(sc - v) / adr if np.isfinite(v) else float("nan")
    records.append({
        "setup": setup, "ticker": ticker, "entry": entry_date,
        "sig_close": sc, "adr": adr, "dists": dists,
    })

print(f"Evaluable: {len(records)}")
print()

MA_NAMES = [f"{k}{n}" for k, n in MA_SET]


def dim_stats(setup_records, ma):
    vals = np.array([r["dists"][ma] for r in setup_records
                     if not np.isnan(r["dists"][ma])])
    if len(vals) < 4:
        return None
    p25 = float(np.percentile(vals, 25))
    p75 = float(np.percentile(vals, 75))
    iqr = p75 - p25
    cushion = IQR_K * iqr
    raw_min = float(np.min(vals))
    raw_max = float(np.max(vals))
    lo = max(0.0, raw_min - cushion)  # clamp at 0 (distance is non-negative)
    hi = raw_max + cushion
    return {
        "n": len(vals), "raw_min": raw_min, "raw_max": raw_max,
        "p25": p25, "p75": p75, "iqr": iqr, "cushion": cushion,
        "box_lo": lo, "box_hi": hi,
    }


print("=" * 100)
print(f"INFLATED BOX per setup per MA (cushion = {IQR_K} * IQR)")
print("=" * 100)
box = {}
for setup in ("htf", "bf", "base"):
    sub = [r for r in records if r["setup"] == setup]
    box[setup] = {}
    print(f"\n{setup.upper()} (n={len(sub)})")
    print(f"  {'MA':<8} {'raw_min':>8} {'raw_max':>8} {'IQR':>7} {'cushion':>8} "
          f"{'box_lo':>8} {'box_hi':>8} {'n_used':>7}")
    for ma in MA_NAMES:
        s = dim_stats(sub, ma)
        box[setup][ma] = s
        if s is None:
            print(f"  {ma:<8}   (insufficient data)")
            continue
        print(f"  {ma:<8} {s['raw_min']:>8.3f} {s['raw_max']:>8.3f} {s['iqr']:>7.3f} "
              f"{s['cushion']:>8.3f} {s['box_lo']:>8.3f} {s['box_hi']:>8.3f} {s['n']:>7}")

print()
print("=" * 100)
print("SANITY — does every example fall inside its setup's inflated box?")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    sub = [r for r in records if r["setup"] == setup]
    b = box[setup]
    fails = []
    for r in sub:
        for ma in MA_NAMES:
            if b[ma] is None: continue
            v = r["dists"][ma]
            if np.isnan(v): continue
            if v < b[ma]["box_lo"] or v > b[ma]["box_hi"]:
                fails.append((r["ticker"], r["entry"], ma, v, b[ma]["box_lo"], b[ma]["box_hi"]))
    print(f"  {setup}: {len(sub) - len(set((f[0], f[1]) for f in fails))}/{len(sub)} examples fully inside box")
    if fails:
        print(f"    (expected 100% by construction — any violation = bug)")
        for f in fails[:5]:
            print(f"      {f[0]} {f[1]}: {f[2]}={f[3]:.3f} outside [{f[4]:.3f}, {f[5]:.3f}]")
print()

print("=" * 100)
print("EXAMPLE BOUNDS SUMMARY (compact — min_raw/max_raw + cushion half-width)")
print("=" * 100)
for setup in ("htf", "bf", "base"):
    b = box[setup]
    print(f"\n{setup.upper()}:")
    for ma in MA_NAMES:
        s = b[ma]
        if s is None: continue
        print(f"  {ma:<8} raw [{s['raw_min']:>6.2f}, {s['raw_max']:>6.2f}]  "
              f"+{s['cushion']:.2f} cushion  =>  inflated [{s['box_lo']:>6.2f}, {s['box_hi']:>6.2f}]")
print()

print("=" * 100)
print("KEY EXAMPLES (QUBT + DRN) — pass/fail per dimension under inflated box")
print("=" * 100)
for tkr in ("QUBT", "DRN"):
    for r in records:
        if r["ticker"] != tkr: continue
        b = box[r["setup"]]
        print(f"\n  {tkr} {r['entry']} ({r['setup']}): sig_close={r['sig_close']:.3f}")
        for ma in MA_NAMES:
            v = r["dists"][ma]
            if np.isnan(v) or b[ma] is None:
                print(f"    {ma:<8} n/a")
                continue
            inside = b[ma]["box_lo"] <= v <= b[ma]["box_hi"]
            tag = "inside" if inside else "OUTSIDE"
            print(f"    {ma:<8} dist={v:.3f}  box=[{b[ma]['box_lo']:.3f}, "
                  f"{b[ma]['box_hi']:.3f}]  {tag}")
