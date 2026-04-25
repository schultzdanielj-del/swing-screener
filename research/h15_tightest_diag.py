"""Show the 3 tightest-corridor examples per setup from h15 to diagnose
how corridor minimums ended up near zero or negative."""
import pickle
import sqlite3
import numpy as np
import pandas as pd

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 100), ("SMA", 200)]
T_FOOTHOLD_CAP = 1.856

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples WHERE setup_type IN ('htf','bf','base')").fetchall()


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
    d = df["date"].dt.strftime("%Y-%m-%d").values
    return d, df["high"].values.astype(float), df["low"].values.astype(float), df["close"].values.astype(float), df["volume"].values.astype(float)


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")
def sma(a, n, i): return float(np.mean(a[i-n+1:i+1])) if i >= n-1 else float("nan")
def ema(a, n, i):
    if i < n-1: return float("nan")
    alpha = 2.0/(n+1); e = a[0]
    for k in range(1, i+1): e = alpha*a[k] + (1-alpha)*e
    return float(e)


def pick_support(c, l, i, adr):
    sl = l[i]; best = None
    for kind, n in MA_SET:
        if i < n-1: continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        if not np.isfinite(v): continue
        fh = abs(sl - v) / adr
        if fh <= T_FOOTHOLD_CAP:
            if best is None or fh < best[0]: best = (fh, kind, n, v)
    return best


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
    best = int(np.argmax(av))
    return int(Ar[best]), float(av[best])


records = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c, v = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue
    sup = pick_support(c, l, i, adr)
    if sup is None: continue
    fh, kind, n, sup_val = sup
    r = argmax_avwap(h, l, c, v, i, n)
    if r is None: continue
    A, av = r
    width = (av - sup_val) / adr
    anchor_bars_back = i - A
    records.append({
        "setup": setup, "ticker": ticker, "entry": entry_date,
        "sig_date": d[i], "sig_close": c[i], "adr": adr,
        "support": f"{kind}{n}", "support_val": sup_val,
        "support_dist": (c[i] - sup_val) / adr,
        "anchor_date": d[A], "anchor_bars_back": anchor_bars_back,
        "avwap": av, "resist_dist": (av - c[i]) / adr,
        "width": width,
    })

print(f"Total evaluable: {len(records)}")
print()
for setup in ("htf", "bf", "base"):
    sub = sorted([r for r in records if r["setup"] == setup], key=lambda r: r["width"])[:3]
    print(f"=== {setup.upper()} — 3 tightest corridors ===")
    for r in sub:
        print(f"  {r['ticker']:<6} {r['entry']}  sig_close={r['sig_close']:.3f}  ADR={r['adr']:.3f}")
        print(f"    support  = {r['support']:<7} @ {r['support_val']:.3f}  (sig_dist {r['support_dist']:+.3f} ADR)")
        print(f"    anchor   = {r['anchor_date']}  ({r['anchor_bars_back']} bars before sig)")
        print(f"    AVWAP    = {r['avwap']:.3f}  (resist_dist {r['resist_dist']:+.3f} ADR)")
        print(f"    width    = {r['width']:+.3f} ADR")
        print()
