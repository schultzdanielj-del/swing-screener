"""Re-score theme EXIT signals by what actually matters: GIVE-BACK (how far below
the high you exit) vs WHIPSAW (shaken out and it makes a new high). Slow MA exits
give back a lot; tight trailing stops give back little but whipsaw more. Find the
knee. 2025, 16 dominant themes. ASCII output."""
import sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

with open(CACHE, "rb") as f:
    cache = pickle.load(f)

def series(tk):
    d = cache[tk].copy(); d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    return d[~d.index.duplicated(keep="last")]["close"].astype(float)

SPY = series("SPY")
CLO = {t: series(t) for t in cache}
BUILD, LEADIN, CYC, DISP = "2025-01-01", "2025-04-01", "2025-05-01", "2025-11-15"

def composite(members):
    cols = []
    for m in members:
        s = CLO.get(m)
        if s is None: continue
        s = s.loc[BUILD:DISP].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

def chord_up(y):
    n = len(y); x = np.arange(n); line = y[0] + (y[-1]-y[0])*(x/(n-1))
    return int(np.argmax(line - y))

def start_date(comp):
    rs = (comp / SPY.reindex(comp.index).ffill()).dropna()
    rs_s = rs.ewm(span=5, adjust=False).mean()
    peak = rs_s.loc[CYC:DISP].idxmax()
    seg = np.log(rs_s.loc[LEADIN:peak].values)
    if len(seg) < 6: return rs_s.loc[LEADIN:peak].index[0]
    return rs_s.loc[LEADIN:peak].index[chord_up(seg)]

# dominant themes
dom = []
for k, mem in THEMES.items():
    c = composite(mem)
    if c is None: continue
    rs = (c / SPY.reindex(c.index).ffill()).dropna()
    if rs.loc[CYC:].empty: continue
    a = rs.asof(pd.Timestamp(CYC))
    if pd.isna(a) or a <= 0: continue
    dom.append((k, mem, c, rs.iloc[-1]/a - 1))
dom.sort(key=lambda r: r[3], reverse=True)
dom = dom[:16]

METHODS = ["EMA21", "10/20bear", "SMA10", "EMA8", "Chandelier2.5ATR", "Chandelier2ATR", "Donch10low"]

def exit_idx(method, vals, ema8, ema21, sma10, sma20, atr, cummax, min10, n):
    for i in range(6, n):
        if method == "EMA21" and vals[i] < ema21[i]: return i
        if method == "EMA8" and vals[i] < ema8[i]: return i
        if method == "SMA10" and vals[i] < sma10[i]: return i
        if method == "10/20bear" and sma10[i] < sma20[i] and sma10[i-1] >= sma20[i-1]: return i
        if method == "Chandelier2.5ATR" and not np.isnan(atr[i]) and vals[i] < cummax[i] - 2.5*atr[i]: return i
        if method == "Chandelier2ATR" and not np.isnan(atr[i]) and vals[i] < cummax[i] - 2.0*atr[i]: return i
        if method == "Donch10low" and not np.isnan(min10[i]) and vals[i] < min10[i]: return i
    return None

agg = {m: dict(gb=[], whip=0, ncall=0, lag=[]) for m in METHODS}
for k, mem, comp, _ in dom:
    s = start_date(comp)
    ema8f = comp.ewm(span=8, adjust=False).mean(); ema21f = comp.ewm(span=21, adjust=False).mean()
    sma10f = comp.rolling(10).mean(); sma20f = comp.rolling(20).mean()
    tr = comp.diff().abs(); atrf = tr.rolling(14).mean()
    min10f = comp.rolling(10).min().shift(1)
    cs = comp.loc[s:DISP]
    idxs = cs.index
    vals = cs.values
    cummax = np.maximum.accumulate(vals)
    def sl(x): return x.reindex(idxs).values
    ema8, ema21, sma10, sma20, atr, min10 = sl(ema8f), sl(ema21f), sl(sma10f), sl(sma20f), sl(atrf), sl(min10f)
    n = len(vals)
    for m in METHODS:
        i = exit_idx(m, vals, ema8, ema21, sma10, sma20, atr, cummax, min10, n)
        if i is None: continue
        runmax = cummax[i]
        gb = (runmax - vals[i]) / runmax * 100
        whip = 1 if (i+1 < n and vals[i+1:].max() > runmax) else 0
        peakpos = int(np.argmax(vals[:i+1]))
        agg[m]["gb"].append(gb); agg[m]["whip"] += whip; agg[m]["ncall"] += 1
        agg[m]["lag"].append(i - peakpos)

print("EXIT BAKE-OFF by GIVE-BACK (2025, 16 themes; trail from takeoff)")
print(f"{'method':17s} {'n':>3s} {'medGiveBack%':>12s} {'whipsaw%':>9s} {'medLag(td)':>10s}")
rows = []
for m in METHODS:
    a = agg[m]
    mg = np.median(a["gb"]) if a["gb"] else np.nan
    wp = a["whip"]/a["ncall"]*100 if a["ncall"] else np.nan
    ml = np.median(a["lag"]) if a["lag"] else np.nan
    rows.append((m, mg, wp, ml, a["ncall"]))
    print(f"{m:17s} {a['ncall']:3d} {mg:12.1f} {wp:9.0f} {ml:10.0f}")
print("\n(lower give-back = exit nearer the high; lower whipsaw = less likely shaken out early)")
print("knee = lowest give-back at a whipsaw rate you can stomach")
