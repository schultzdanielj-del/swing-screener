"""Pull GLP-1 / obesity out as its own composite (it's buried inside Biotech in the
theme_map) and see when it was hot and where it would rank vs the tracked themes.
2023->now, RS vs SPY. ASCII."""
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
A, B = "2022-09-01", "2026-06-05"

GLP1 = ["LLY", "NVO", "VKTX", "HIMS", "ALT", "TERN", "STOK"]
print("GLP-1 members present:", [t for t in GLP1 if t in cache and series(t) is not None])

def composite(members):
    cols = []
    for m in members:
        s = CLO.get(m) if m in CLO else series(m)
        if s is None: continue
        s = s.loc[A:B].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

spy_m = SPY.loc[A:B].resample("ME").last().pct_change() * 100
def monthly_rs(c):
    return (c.resample("ME").last().pct_change() * 100 - spy_m)

# all themes + GLP-1
rs = {}
for k, mem in THEMES.items():
    c = composite(mem)
    if c is not None: rs[THEME_LABELS.get(k, k)] = monthly_rs(c)
glp = composite(GLP1)
rs["** GLP-1 / obesity (LLY,NVO,VKTX..)"] = monthly_rs(glp)
M = pd.DataFrame(rs).loc["2023-01-01":]

total = M.sum().sort_values(ascending=False)
rank = list(total.index).index("** GLP-1 / obesity (LLY,NVO,VKTX..)") + 1
print(f"\nGLP-1 total RS vs SPY 2023->now: {total['** GLP-1 / obesity (LLY,NVO,VKTX..)']:+.0f}  "
      f"=> rank {rank} of {len(total)} themes")
print("(for scale: #1 Crypto Miners ~+359; biotech bucket total:",
      f"{total.get('Biotech  therapeutics, gene editing, GLP-1', float('nan')):+.0f})")

print("\nGLP-1 RS by half-year (and its rank that half among all themes):")
for lo, hi, lbl in [("2023-01","2023-06","2023 H1"),("2023-07","2023-12","2023 H2"),
                    ("2024-01","2024-06","2024 H1"),("2024-07","2024-12","2024 H2"),
                    ("2025-01","2025-06","2025 H1"),("2025-07","2025-12","2025 H2"),
                    ("2026-01","2026-06","2026 H1")]:
    seg = M.loc[lo:hi].sum().sort_values(ascending=False)
    g = "** GLP-1 / obesity (LLY,NVO,VKTX..)"
    r = list(seg.index).index(g) + 1
    print(f"  {lbl}: GLP-1 RS {seg[g]:+6.0f}  (rank {r}/{len(seg)})")
