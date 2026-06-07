"""Diagnostic: Robotics & Automation theme composite 2025 with 10/20/50 SMA + 21 EMA,
start/peak/end markers and the 10/20 bear cross, plus the member list + coverage so the
composite can be verified by eye. ASCII output."""
import sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT = REPO / "research" / "robotics_diag_2025.png"

with open(CACHE, "rb") as f:
    cache = pickle.load(f)

def series(tk):
    d = cache[tk].copy(); d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    return d[~d.index.duplicated(keep="last")]["close"].astype(float)

SPY = series("SPY")
BUILD, DISP = "2025-01-01", "2025-12-15"

key = next(k for k, v in THEME_LABELS.items() if v.lower().startswith("robotics"))
members = THEMES[key]
print(f"theme key: {key}  label: {THEME_LABELS[key]}")
print(f"listed members ({len(members)}):")
cols = []
for m in members:
    if m not in cache:
        print(f"   {m:6s}  NOT IN CACHE"); continue
    s = series(m).loc[BUILD:DISP].dropna()
    used = s.shape[0] >= 60 and s.iloc[0] > 0
    print(f"   {m:6s}  {('USED ' if used else 'skip ')} bars={s.shape[0]:3d} "
          f"first={s.index[0].date() if len(s) else '--'}")
    if used:
        cols.append(s / s.iloc[0] * 100)

comp = pd.concat(cols, axis=1)
need = max(1, comp.shape[1] // 2)
comp = comp.mean(axis=1, skipna=True).where(comp.notna().sum(axis=1) >= need).dropna()
print(f"\ncomposite built from {len(cols)} members, {comp.index[0].date()} -> {comp.index[-1].date()}")

sma10, sma20, sma50 = comp.rolling(10).mean(), comp.rolling(20).mean(), comp.rolling(50).mean()
ema21 = comp.ewm(span=21, adjust=False).mean()

# peak (highest close), then first 10/20 bear cross after it
cyc = comp.loc["2025-05-01":"2025-11-15"]
ptop = cyc.idxmax()
after = comp.loc[ptop:]
cross = None
for j in range(1, len(after)):
    d, dp = after.index[j], after.index[j-1]
    if sma10.loc[d] < sma20.loc[d] and sma10.loc[dp] >= sma20.loc[dp]:
        cross = d; break
end = comp.loc["2025-04-01":cross].idxmax() if cross is not None else None

print(f"\nhighest close (peak): {ptop.date()}  = {comp.loc[ptop]:.1f}")
print(f"first 10/20 bear cross after peak: {cross.date() if cross is not None else 'none'}")
print(f"END (highest close before cross): {end.date() if end is not None else 'open'}")

# plot
p = comp.loc["2025-03-01":DISP]
fig, ax = plt.subplots(figsize=(15, 8), facecolor="black"); ax.set_facecolor("black")
ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.15)
ax.plot(p.index, p.values, color="white", lw=1.6, label="Robotics composite (EW, rebased)")
ax.plot(sma10.loc["2025-03-01":].index, sma10.loc["2025-03-01":].values, color="#5fc8ff", lw=1.0, label="SMA10")
ax.plot(sma20.loc["2025-03-01":].index, sma20.loc["2025-03-01":].values, color="#ff9500", lw=1.0, label="SMA20")
ax.plot(sma50.loc["2025-03-01":].index, sma50.loc["2025-03-01":].values, color="#ffcc00", lw=1.0, label="SMA50")
if end is not None:
    ax.axvline(end, color="#ff3030", lw=1.4, ls="-"); ax.scatter([end], [comp.loc[end]], color="#ff3030", s=90, zorder=6)
    ax.text(end, comp.loc[end], "  END/top", color="#ff3030", fontsize=9, va="bottom")
if cross is not None:
    ax.axvline(cross, color="#ffffff", lw=1.0, ls="--")
    ax.text(cross, p.min(), " 10/20 bear cross", color="#ddd", fontsize=8, rotation=90, va="bottom")
ax.legend(loc="upper left", fontsize=9, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
ax.set_title(f"Robotics & Automation composite 2025  ({len(cols)} members)", color="white")
ax.xaxis.set_major_locator(mdates.MonthLocator()); ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=135, bbox_inches="tight")
print(f"\nsaved: {OUT}")
