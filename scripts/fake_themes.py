"""Count 'fake themes' -- ones that emerged (RS chord-elbow takeoff) but barely lasted
before a 10/20 SMA bear cross killed them. Run over the WHOLE theme universe for 2025
and 2026. Reports the lifespan distribution + lists the short-lived ones. No thresholds
baked in -- shows the distribution so the cut is visible. ASCII output."""
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

def composite(members, a, b):
    cols = []
    for m in members:
        s = CLO.get(m)
        if s is None: continue
        s = s.loc[a:b].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

def chord_up(y):
    n = len(y); x = np.arange(n); line = y[0] + (y[-1]-y[0])*(x/(n-1))
    return int(np.argmax(line - y))

def analyze(build, leadin, cyc, disp, label):
    print("=" * 78); print(f"{label}: span of EVERY theme (takeoff -> 10/20 bear cross)"); print("=" * 78)
    rows = []
    nbuilt = 0
    for k, mem in THEMES.items():
        comp = composite(mem, build, disp)
        if comp is None or comp.loc[cyc:disp].empty: continue
        nbuilt += 1
        rs = (comp / SPY.reindex(comp.index).ffill()).dropna()
        rs_s = rs.ewm(span=5, adjust=False).mean()
        rs_peak = rs_s.loc[cyc:disp].idxmax()
        seg = np.log(rs_s.loc[leadin:rs_peak].values)
        start = rs_s.loc[leadin:rs_peak].index[chord_up(seg)] if len(seg) >= 6 else rs_s.loc[leadin:rs_peak].index[0]
        sma10, sma20 = comp.rolling(10).mean(), comp.rolling(20).mean()
        ptop = comp.loc[cyc:disp].idxmax()
        after = comp.loc[ptop:disp]
        cross = None
        for j in range(1, len(after)):
            d, dp = after.index[j], after.index[j-1]
            if sma10.loc[d] < sma20.loc[d] and sma10.loc[dp] >= sma20.loc[dp]:
                cross = d; break
        if cross is None:
            rows.append(dict(lbl=THEME_LABELS.get(k, k), start=start, life=None, leg=np.nan)); continue
        life = comp.loc[start:cross].shape[0] - 1                      # trading days takeoff -> bear cross
        top = comp.loc[start:cross].idxmax()
        leg = (comp.loc[top] / comp.loc[start] - 1) * 100              # advance of the leg, %
        rows.append(dict(lbl=THEME_LABELS.get(k, k), start=start, life=life, leg=leg, end=top))

    died = [r for r in rows if r["life"] is not None]
    alive = [r for r in rows if r["life"] is None]
    lives = np.array([r["life"] for r in died])
    print(f"themes built: {nbuilt}   |   confirmed a top+death: {len(died)}   |   still up at cycle end: {len(alive)}")
    if len(lives):
        print(f"lifespan (takeoff->bear cross) trading days:  min {lives.min()}  median {int(np.median(lives))}  max {lives.max()}")
        for lo, hi in [(0, 10), (11, 20), (21, 40), (41, 9999)]:
            c = int(((lives >= lo) & (lives <= hi)).sum())
            tag = "  <-- 'fake' / barely lasted" if hi <= 20 else ""
            print(f"   lasted {lo}-{hi if hi<9999 else '+'} td: {c}{tag}")
    print("\nSHORT-LIVED themes (lasted <= 20 td), sorted by lifespan:")
    print(f"{'life(td)':>8s} {'leg%':>6s} {'takeoff':12s} {'top':12s}  theme")
    for r in sorted([r for r in died if r["life"] <= 20], key=lambda x: x["life"]):
        print(f"{r['life']:8d} {r['leg']:6.0f} {str(r['start'].date()):12s} {str(r['end'].date()):12s}  "
              f"{r['lbl'].encode('ascii','ignore').decode().strip()}")

analyze("2025-01-01", "2025-04-01", "2025-05-01", "2025-11-15", "2025 CYCLE")
print()
analyze("2025-11-01", "2026-03-01", "2026-04-08", "2026-06-05", "2026 CYCLE")
