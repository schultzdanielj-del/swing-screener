"""Are financials/regional banks rotating in post-x-ADR-peak the way the defensives
are? Pre vs post-peak RS-vs-SPY for KRE, XLF (direct ETFs) + the financial themes,
with defensives (managed care, diagnostics) as a reference. 2025 vs 2026. ASCII."""
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
    if tk not in cache: return None
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

def asof(s, dt):
    v = s.asof(dt); return v if pd.notna(v) else np.nan

def rs_ret(comp, a, b):
    ra = asof(comp, a) / asof(SPY, a); rb = asof(comp, b) / asof(SPY, b)
    return (rb / ra - 1) * 100 if (pd.notna(ra) and pd.notna(rb) and ra != 0) else np.nan

DIRECT = {"KRE (regional banks)": "KRE", "XLF (broad financials)": "XLF"}
TH_MATCH = ["fintech", "financial services", "alt asset", "managed care", "diagnostics"]

def th_comp(keyword, build, disp):
    for k, mem in THEMES.items():
        if keyword in THEME_LABELS.get(k, k).lower():
            return composite(mem, build, disp), THEME_LABELS.get(k, k)
    return None, None

def run(build, cyc, disp, pk, label):
    print("=" * 76); print(label); print("=" * 76)
    print(f"{'preRS%':>7s} {'postRS%':>8s}  rotation            name")
    rows = []
    for name, tk in DIRECT.items():
        s = series(tk)
        if s is None: rows.append((name, np.nan, np.nan)); continue
        rows.append((name, rs_ret(s, pd.Timestamp(cyc), pd.Timestamp(pk)),
                     rs_ret(s, pd.Timestamp(pk), pd.Timestamp(disp))))
    for kw in TH_MATCH:
        comp, lbl = th_comp(kw, build, disp)
        if comp is None: continue
        rows.append((lbl.encode("ascii", "ignore").decode().strip(),
                     rs_ret(comp, pd.Timestamp(cyc), pd.Timestamp(pk)),
                     rs_ret(comp, pd.Timestamp(pk), pd.Timestamp(disp))))
    for name, pre, post in sorted(rows, key=lambda r: (-(r[2]-r[1]) if not np.isnan(r[1]) else 1e9)):
        if np.isnan(pre):
            print(f"{'--':>7s} {'--':>8s}  not in cache         {name}"); continue
        rot = "IN (lagged->led)" if (pre < 5 and post > 5) else ("led both" if pre > 5 and post > 5 else "weak/fading")
        print(f"{pre:+7.0f} {post:+8.0f}  {rot:18s}  {name}")

run("2025-01-01", "2025-05-01", "2025-11-15", "2025-07-03",
    "2025  (x-ADR peak 7/3; pre=5/1->7/3, post=7/3->end)")
print()
run("2025-11-01", "2026-04-08", "2026-06-05", "2026-05-14",
    "2026  (x-ADR peak 5/14; pre=4/8->5/14, post=5/14->6/5)")
