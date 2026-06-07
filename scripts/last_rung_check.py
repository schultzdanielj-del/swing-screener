"""Is the AI supply chain finding a LAST RUNG (gas/water/datacenter-land) post-5/14,
or has it topped and only the off-chain scatter (defensives/banks/shipping) is bid?
Pre vs post-x-ADR-peak RS vs SPY, 2026. ASCII."""
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
A, B = "2025-11-01", "2026-06-05"
CYC, PK, DISP = pd.Timestamp("2026-04-08"), pd.Timestamp("2026-05-14"), pd.Timestamp("2026-06-05")

def composite(members):
    cols, present = [], []
    for m in members:
        s = series(m)
        if s is None: continue
        s = s.loc[A:B].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100); present.append(m)
    if not cols: return None, []
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna(), present

def th_comp(keyword):
    for k, mem in THEMES.items():
        if keyword in THEME_LABELS.get(k, k).lower():
            c, _ = composite(mem); return c
    return None

def rs_ret(comp, a, b):
    ra = comp.asof(a) / SPY.asof(a); rb = comp.asof(b) / SPY.asof(b)
    return (rb / ra - 1) * 100 if (pd.notna(ra) and pd.notna(rb) and ra) else np.nan

LAST_RUNG = {
    "Natural gas / power-fuel": ["EQT","AR","RRC","LNG","CTRA","EXE","UNG","CNX"],
    "Water (cooling input)":    ["AWK","WTRG","XYL","AWR","CWT","PHO"],
    "Datacenter land / REITs":  ["EQIX","DLR","IRM","COR"],
}
print("ad-hoc composite membership present in cache:")
groups = []
for name, tks in LAST_RUNG.items():
    c, pres = composite(tks)
    print(f"  {name}: {pres}")
    if c is not None: groups.append(("LAST RUNG", name, c))
# in-chain reference (current bottom of chain) + off-chain scatter
for kw, nm in [("critical minerals", "Critical Minerals (in-chain ref)"),
               ("industrial metals", "Industrial Metals (in-chain ref)")]:
    c = th_comp(kw)
    if c is not None: groups.append(("IN-CHAIN", nm, c))
for kw, nm in [("managed care", "Managed Care (off-chain)"),
               ("transports", "Shipping/Transports (off-chain)")]:
    c = th_comp(kw)
    if c is not None: groups.append(("OFF-CHAIN", nm, c))
kre = series("KRE")
if kre is not None:
    groups.append(("OFF-CHAIN", "KRE regional banks (off-chain)", kre.loc[A:B]))

print("\n2026: pre = 4/8->5/14 (into peak), post = 5/14->6/5 (since peak)")
print(f"{'cat':10s} {'preRS%':>7s} {'postRS%':>8s}  rotating in?  name")
rows = []
for cat, nm, c in groups:
    pre, post = rs_ret(c, CYC, PK), rs_ret(c, PK, DISP)
    rows.append((cat, nm, pre, post))
for cat, nm, pre, post in sorted(rows, key=lambda r: -(r[3] - r[2])):
    rot = "YES" if (pre < 5 and post > 5) else ("turning" if post > 2 else "no")
    print(f"{cat:10s} {pre:+7.0f} {post:+8.0f}  {rot:12s}  {nm}")
