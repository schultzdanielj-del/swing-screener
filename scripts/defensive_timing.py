"""When do defensive/hedge themes (managed care, gold, discount retail, medtech,
diagnostics) emerge relative to the x-ADR peak in each cycle? 2025 (full) vs 2026
(in-progress). RS takeoff = chord-elbow; also P1/P2 RS to show the weak->strong
defensive-rotation signature. ASCII output."""
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

DEF_KEYS = ["managed care", "gold", "discount retail", "medtech", "diagnostics",
            "transports", "consulting"]  # defensives + the idiosyncratic comparison
def is_def(lbl): return any(k in lbl.lower() for k in DEF_KEYS)

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

def asof(s, dt):
    v = s.asof(dt); return v if pd.notna(v) else np.nan

def takeoff(comp, leadin, cyc, disp):
    rs = (comp / SPY.reindex(comp.index).ffill()).dropna()
    rs_s = rs.ewm(span=5, adjust=False).mean()
    peak = rs_s.loc[cyc:disp].idxmax()
    seg = np.log(rs_s.loc[leadin:peak].values)
    return rs_s.loc[leadin:peak].index[chord_up(seg)] if len(seg) >= 6 else rs_s.loc[leadin:peak].index[0]

def rs_ret(comp, a, b):
    rs_a = asof(comp, a) / asof(SPY, a); rs_b = asof(comp, b) / asof(SPY, b)
    return (rs_b / rs_a - 1) * 100

def run(build, cyc, disp, xadr_peak, label):
    print("=" * 80); print(f"{label}  (x-ADR peak {xadr_peak})"); print("=" * 80)
    pk = pd.Timestamp(xadr_peak)
    print(f"{'preRS%':>7s} {'postRS%':>8s}  rotation  theme")
    rows = []
    for k, mem in THEMES.items():
        lbl = THEME_LABELS.get(k, k)
        if not is_def(lbl): continue
        comp = composite(mem, build, disp)
        if comp is None or comp.loc[cyc:disp].empty: continue
        pre = rs_ret(comp, pd.Timestamp(cyc), pk)        # cycle start -> x-ADR peak
        post = rs_ret(comp, pk, pd.Timestamp(disp))      # x-ADR peak -> now/end
        rows.append((lbl, pre, post))
    for lbl, pre, post in sorted(rows, key=lambda r: r[2] - r[1], reverse=True):
        rot = "IN (lagged->led)" if (pre < 5 and post > 5) else ("led both" if pre > 5 and post > 5 else "weak/fading")
        print(f"{pre:+7.0f} {post:+8.0f}  {rot:18s}  {lbl.encode('ascii','ignore').decode().strip()}")

run("2025-01-01", "2025-05-01", "2025-11-15", "2025-07-03", "2025 CYCLE  (reset 8/1; pre=5/1->7/3, post=7/3->end)")
print()
run("2025-11-01", "2026-04-08", "2026-06-05", "2026-05-14", "2026 CYCLE  (now 6/5; pre=4/8->5/14, post=5/14->6/5)")
