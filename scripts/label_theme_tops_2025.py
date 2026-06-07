"""Label each theme's TOP in hindsight = highest closing value before the first
10/20 SMA bear cross that follows it. The cross only CONFIRMS the top happened; the
label is the peak itself (no give-back). Also shows how far the cross lags the top in
price terms (why the cross is only a confirmer, not the marker). 2025. ASCII output."""
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
BUILD, CYC, DISP = "2025-01-01", "2025-05-01", "2025-11-15"

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

# dominant themes by cycle RS
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

print("LABELED THEME TOPS 2025  (top = highest close before the confirming 10/20 bear cross)")
print(f"{'TOP date':12s} {'topClose':>9s} {'cross date':12s} {'drop top->cross':>15s}  theme")
labels = []
for k, mem, comp, _ in dom:
    cyc = comp.loc[CYC:DISP]
    sma10 = comp.rolling(10).mean(); sma20 = comp.rolling(20).mean()
    top_date = cyc.idxmax(); top_close = cyc.max()
    # first 10/20 bear cross strictly after the top
    cross_date = None
    after = comp.loc[top_date:DISP]
    for j in range(1, len(after)):
        d = after.index[j]
        if sma10.loc[d] < sma20.loc[d] and sma10.loc[after.index[j-1]] >= sma20.loc[after.index[j-1]]:
            cross_date = d; break
    if cross_date is None:
        print(f"{str(top_date.date()):12s} {top_close:9.1f} {'(no cross)':12s} {'still up':>15s}  "
              f"{THEME_LABELS.get(k,k).encode('ascii','ignore').decode().strip()}")
        labels.append((k, top_date, None)); continue
    drop = (comp.loc[cross_date] - top_close) / top_close * 100
    print(f"{str(top_date.date()):12s} {top_close:9.1f} {str(cross_date.date()):12s} {drop:14.1f}%  "
          f"{THEME_LABELS.get(k,k).encode('ascii','ignore').decode().strip()}")
    labels.append((k, top_date, cross_date))

confirmed = [l for l in labels if l[2] is not None]
print(f"\n{len(confirmed)}/{len(labels)} themes have a confirmed top; "
      f"median drop from top to the 10/20 cross = "
      f"{np.median([ (composite(dict(THEMES)[k]).loc[c] / composite(dict(THEMES)[k]).loc[t] -1)*100 for k,t,c in confirmed]):.1f}% "
      "(this is the give-back you'd eat if you used the cross as the exit -- which is exactly why we label at the top instead)")
