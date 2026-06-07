"""Around the 2025 range expansion (8/1): how many themes flashed strength (a 10/20
SMA bull cross in 7/28-8/31) and then fizzled (bear-crossed back within <=20 td) vs
held into the cycle. Whole theme universe. ASCII output."""
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
BUILD, DISP = "2025-01-01", "2025-11-15"
WIN_A, WIN_B = pd.Timestamp("2025-07-28"), pd.Timestamp("2025-08-31")  # around the range expansion
FIZZLE_TD = 20

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

built = 0; flashed = []; no_flash = 0
for k, mem in THEMES.items():
    comp = composite(mem)
    if comp is None: continue
    built += 1
    sma10, sma20 = comp.rolling(10).mean(), comp.rolling(20).mean()
    idx = comp.index
    pos = {d: i for i, d in enumerate(idx)}
    # first 10/20 BULL cross inside the window
    bull = None
    win = comp.loc[WIN_A:WIN_B].index
    for d in win:
        i = pos[d]
        if i >= 1 and sma10.iloc[i] > sma20.iloc[i] and sma10.iloc[i-1] <= sma20.iloc[i-1]:
            bull = i; break
    if bull is None:
        no_flash += 1; continue
    # next BEAR cross after the bull cross
    bear = None
    for i in range(bull+1, len(idx)):
        if sma10.iloc[i] < sma20.iloc[i] and sma10.iloc[i-1] >= sma20.iloc[i-1]:
            bear = i; break
    life = (bear - bull) if bear is not None else None
    flashed.append(dict(lbl=THEME_LABELS.get(k, k), bull=idx[bull], life=life))

fiz = [f for f in flashed if f["life"] is not None and f["life"] <= FIZZLE_TD]
held = [f for f in flashed if f["life"] is None or f["life"] > FIZZLE_TD]
print(f"themes built: {built}")
print(f"flashed strength around the range expansion (10/20 bull cross 7/28-8/31): {len(flashed)}")
print(f"   -> FIZZLED (bear cross within {FIZZLE_TD} td): {len(fiz)}")
print(f"   -> HELD (lasted > {FIZZLE_TD} td or no bear cross): {len(held)}")
if flashed:
    print(f"   fizzle rate among flashers: {len(fiz)/len(flashed)*100:.0f}%")
print(f"did NOT flash a bull cross in the window: {no_flash}")

print("\nFIZZLERS (flashed then died <= 20 td), by lifespan:")
for f in sorted(fiz, key=lambda x: x["life"]):
    print(f"   bull {str(f['bull'].date())}  life {f['life']:2d} td  {f['lbl'].encode('ascii','ignore').decode().strip()}")
print("\nHELD (flashed and stuck):")
for f in sorted(held, key=lambda x: (x['life'] is not None, x['life'] if x['life'] is not None else 999)):
    lf = f"{f['life']} td" if f["life"] is not None else "no bear cross"
    print(f"   bull {str(f['bull'].date())}  {lf:14s}  {f['lbl'].encode('ascii','ignore').decode().strip()}")
