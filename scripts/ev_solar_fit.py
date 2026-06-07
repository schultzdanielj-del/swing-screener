"""Where do EV Makers and Solar sit? Total + half-year RS vs SPY (leaders or laggards?)
and monthly-RS correlation with the AI/power legs vs each other (aligned or off on
their own?). 2023->now. ASCII."""
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
A, B = "2022-09-01", "2026-06-05"

def composite(members):
    cols = []
    for m in members:
        s = CLO.get(m)
        if s is None: continue
        s = s.loc[A:B].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

spy_m = SPY.loc[A:B].resample("ME").last().pct_change() * 100
def mrs(c): return (c.resample("ME").last().pct_change() * 100 - spy_m)

def get(kw):
    for k, mem in THEMES.items():
        if kw in THEME_LABELS.get(k, k).lower():
            return mrs(composite(mem))
    return None

NAMES = {
    "EV Makers": "ev makers", "Solar": "solar",
    "EV Supply Chain": "ev supply", "Power Demand for AI": "power demand",
    "Datacenter Buildout": "datacenter buildout", "Nuclear": "nuclear",
    "Critical Minerals": "critical minerals", "Energy Storage": "energy storage",
    "AI Compute (semis)": "ai compute  semis",
}
S = {nm: get(kw) for nm, kw in NAMES.items()}
M = pd.DataFrame({k: v for k, v in S.items() if v is not None}).loc["2023-01-01":]

print("TOTAL & HALF-YEAR RS vs SPY (leaders vs laggards):")
for tgt in ["EV Makers", "Solar"]:
    tot = M[tgt].sum()
    halves = []
    for lo, hi, lbl in [("2023-01","2023-12","2023"),("2024-01","2024-06","24H1"),
                        ("2024-07","2024-12","24H2"),("2025-01","2025-06","25H1"),
                        ("2025-07","2025-12","25H2"),("2026-01","2026-06","26H1")]:
        halves.append(f"{lbl} {M[tgt].loc[lo:hi].sum():+.0f}")
    # rank among all 74 themes
    print(f"  {tgt:10s} total {tot:+.0f}   " + "  ".join(halves))

print("\nMONTHLY-RS CORRELATION (do they move WITH the AI/power legs, or off on their own?):")
print(f"{'':22s} " + " ".join(f"{n[:7]:>7s}" for n in ["EV Mk", "Solar"]))
for nm in M.columns:
    cev = M[nm].corr(M["EV Makers"]); cso = M[nm].corr(M["Solar"])
    print(f"  {nm:22s} {cev:+7.2f} {cso:+7.2f}")
