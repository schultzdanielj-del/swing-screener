"""H18: per-setup bounding box of signal bar's |sig_close - MA| / ADR distances.

Concept (Dan 2026-04-23):
For each example, compute the unsigned ADR distance from sig_close to each of
the 6 MAs in the set. Per setup, take the min/max of each dimension across
examples -> 6-dim bounding box. A wild signal is "ready" if all 6 of its
distances fall inside the setup's box.

By construction, 100% of examples pass their setup's box.

MA set (Dan-locked 2026-04-23): {EMA3, EMA8, EMA21, SMA50, SMA200, SMA330}.
SMA100 dropped (not an institutional level); SMA330 added.
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 200), ("SMA", 330)]

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
print(f"DB examples: {len(rows)}")
print()


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
skipped_missing_ma = 0
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
    missing = False
    for kind, n in MA_SET:
        if i < n-1:
            dists[f"{kind}{n}"] = float("nan")
            missing = True
            continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        dists[f"{kind}{n}"] = abs(sc - v) / adr if np.isfinite(v) else float("nan")
    if missing:
        skipped_missing_ma += 1
    records.append({
        "setup": setup, "ticker": ticker, "entry": entry_date,
        "sig_close": sc, "adr": adr,
        "dists": dists, "missing": missing,
    })

print(f"Evaluable: {len(records)} (with at least one MA missing: {skipped_missing_ma})")
print()

MA_NAMES = [f"{k}{n}" for k, n in MA_SET]


def bbox(setup_records, ma_name):
    vals = [r["dists"][ma_name] for r in setup_records
            if not np.isnan(r["dists"][ma_name])]
    if not vals: return None
    return (np.min(vals), np.median(vals), np.max(vals), len(vals))


print("=" * 90)
print("PER-SETUP BOUNDING BOX — |sig_close - MA| / ADR")
print("=" * 90)
for setup in ("htf", "bf", "base"):
    sub = [r for r in records if r["setup"] == setup]
    print(f"\n{setup.upper()} (n={len(sub)} examples)")
    print(f"  {'MA':<8} {'min':>7} {'p50':>7} {'max':>7} {'n_valid':>8} {'n_missing':>10}")
    for ma in MA_NAMES:
        b = bbox(sub, ma)
        if b is None:
            print(f"  {ma:<8} {'---':>7} {'---':>7} {'---':>7} {0:>8} {len(sub):>10}")
            continue
        mn, md, mx, n_valid = b
        n_missing = len(sub) - n_valid
        print(f"  {ma:<8} {mn:>7.3f} {md:>7.3f} {mx:>7.3f} {n_valid:>8} {n_missing:>10}")
print()

# Ceiling-setters per MA per setup (the example driving the upper bound)
print("=" * 90)
print("CEILING-SETTERS (example at the max of each MA dimension per setup)")
print("=" * 90)
for setup in ("htf", "bf", "base"):
    sub = [r for r in records if r["setup"] == setup]
    print(f"\n{setup.upper()}")
    for ma in MA_NAMES:
        vals = [(r, r["dists"][ma]) for r in sub if not np.isnan(r["dists"][ma])]
        if not vals: continue
        top = max(vals, key=lambda x: x[1])
        r, v = top
        print(f"  {ma:<8} max={v:.3f}  {r['ticker']} {r['entry']}")
print()

# Correlation between dimensions — do they carry independent info or mostly redundant?
print("=" * 90)
print("DIMENSION CORRELATION MATRIX (all 110 examples pooled)")
print("=" * 90)
valid_records = [r for r in records if not r["missing"]]
if valid_records:
    arr = np.array([[r["dists"][ma] for ma in MA_NAMES] for r in valid_records])
    corr = np.corrcoef(arr.T)
    print(f"  (n={len(valid_records)} with all 6 MAs present)")
    print("         " + "  ".join(f"{ma:>7}" for ma in MA_NAMES))
    for i, ma in enumerate(MA_NAMES):
        print(f"  {ma:<7}" + "  ".join(f"{corr[i,j]:>7.2f}" for j in range(len(MA_NAMES))))
print()

# QUBT and DRN specifically
print("=" * 90)
print("KEY EXAMPLES")
print("=" * 90)
for tkr in ("QUBT", "DRN"):
    for r in records:
        if r["ticker"] != tkr: continue
        print(f"  {tkr} {r['entry']} ({r['setup']}): sig_close={r['sig_close']:.3f} ADR={r['adr']:.3f}")
        for ma in MA_NAMES:
            v = r["dists"][ma]
            vs = f"{v:.3f}" if not np.isnan(v) else "NaN"
            print(f"    |sig_close - {ma}| / ADR = {vs}")
