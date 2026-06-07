"""Validate the 'post-peak -> defensive rotation' pattern across 2023-2024.
Find SPY x-ADR peaks (momentum tops), check the pullback after each, and measure
whether the defensive cohort outperformed in the ~6 weeks after the peak. ASCII."""
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

def frame(tk):
    d = cache[tk].copy(); d["date"] = pd.to_datetime(d["date"])
    return d.sort_values("date").set_index("date")[~d.set_index("date").index.duplicated()]

spd = frame("SPY")
SPYc = spd["close"].astype(float)
sma50 = SPYc.rolling(50).mean()
adr_pct = 100 * ((spd["high"].astype(float) / spd["low"].astype(float)).rolling(20).mean() - 1)
xadr = ((SPYc / sma50 - 1) * 100) / adr_pct

def close(tk):
    if tk not in cache: return None
    return frame(tk)["close"].astype(float)
CLO = {t: close(t) for t in cache}
SPYclose = CLO["SPY"]

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

DEF_KW = ["managed care", "gold", "diagnostics", "medtech"]
BUILD, BEND = "2022-06-01", "2025-01-31"
def_comps = []
for k, mem in THEMES.items():
    if any(kw in THEME_LABELS.get(k, k).lower() for kw in DEF_KW):
        c = composite(mem, BUILD, BEND)
        if c is not None:
            def_comps.append((THEME_LABELS.get(k, k), c))
print(f"defensive cohort: {[n.split('  ')[0].split(chr(183))[0].strip() for n,_ in def_comps]}")

# x-ADR peaks in 2023-2024
xs = xadr.ewm(span=5, adjust=False).mean()
win = xs.loc["2023-01-01":"2024-12-31"]
idx = xs.index
vals = xs.values
pos = {d: i for i, d in enumerate(idx)}
W, THRESH = 20, 3.0
peaks = []
for d in win.index:
    i = pos[d]
    if i < W or i > len(vals) - W: continue
    if vals[i] == np.nanmax(vals[i-W:i+W+1]) and vals[i] > THRESH:
        peaks.append(i)
# dedupe within 30 td (keep higher)
ded = []
for i in peaks:
    if ded and i - ded[-1] < 30:
        if vals[i] > vals[ded[-1]]: ded[-1] = i
    else:
        ded.append(i)

def rs_ret(comp, a, b):
    ra = comp.asof(a) / SPYclose.asof(a); rb = comp.asof(b) / SPYclose.asof(b)
    return (rb / ra - 1) * 100 if (pd.notna(ra) and pd.notna(rb) and ra) else np.nan

def td_off(i, n):  # date n trading days from index i
    j = min(max(i + n, 0), len(idx) - 1); return idx[j]

print(f"\n{'peak':12s} {'xADR':>5s} {'SPY drop +30td':>14s} {'def preRS':>10s} {'def postRS':>11s}  rotated in?")
hits = 0
for i in ded:
    pk = idx[i]
    pre_a, post_b = idx[max(0, i-25)], td_off(i, 30)
    # SPY pullback in next 30 td
    fwd = SPYclose.loc[pk:post_b]
    dd = (fwd.min() / SPYclose.loc[pk] - 1) * 100 if len(fwd) else np.nan
    pre = np.nanmean([rs_ret(c, pre_a, pk) for _, c in def_comps])
    post = np.nanmean([rs_ret(c, pk, post_b) for _, c in def_comps])
    rot = "YES" if (post > pre and post > 3) else "no"
    if rot == "YES": hits += 1
    print(f"{str(pk.date()):12s} {vals[i]:5.1f} {dd:13.1f}% {pre:+10.1f} {post:+11.1f}  {rot}")

print(f"\npeaks found: {len(ded)}   defensive rotation after peak: {hits}/{len(ded)}")
print("(2025 reference: 7/3 peak -> defensives rotated in = YES)")
