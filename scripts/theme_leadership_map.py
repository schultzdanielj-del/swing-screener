"""Map every theme's strength vs SPY, month by month, 2023->now. Heatmap sorted by
when each theme peaked (rotation = diagonal). Marks SPY x-ADR peaks. Prints the
total-RS leaderboard + top themes per half-year. ASCII output + PNG."""
import sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"
OUT = REPO / "research" / "theme_leadership_map.png"

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
rs = {}
for k, mem in THEMES.items():
    c = composite(mem)
    if c is None: continue
    cm = c.resample("ME").last().pct_change() * 100
    rs[THEME_LABELS.get(k, k)] = (cm - spy_m)
M = pd.DataFrame(rs)                      # months x themes, monthly RS%
M = M.loc["2023-01-01":]
roll3 = M.rolling(3).sum()               # 3-month cumulative RS (smoother)

# include themes that were ever genuinely hot; sort by when they peaked
maxhot = roll3.max()
include = maxhot.sort_values(ascending=False).head(48).index
peak_month = {t: int(np.nanargmax(roll3[t].values)) for t in include}
order = sorted(include, key=lambda t: peak_month[t])

# ---- leaderboards ----
total = M.sum().sort_values(ascending=False)
print("TOTAL RS vs SPY, 2023->now (top 15 / bottom 8):")
for t in total.head(15).index: print(f"  {total[t]:+7.0f}  {t.encode('ascii','ignore').decode().strip()}")
print("  ...")
for t in total.tail(8).index: print(f"  {total[t]:+7.0f}  {t.encode('ascii','ignore').decode().strip()}")

print("\nHOTTEST themes per half-year (top 5 by RS):")
for lo, hi, lbl in [("2023-01","2023-06","2023 H1"),("2023-07","2023-12","2023 H2"),
                    ("2024-01","2024-06","2024 H1"),("2024-07","2024-12","2024 H2"),
                    ("2025-01","2025-06","2025 H1"),("2025-07","2025-12","2025 H2"),
                    ("2026-01","2026-06","2026 H1")]:
    seg = M.loc[lo:hi].sum().sort_values(ascending=False)
    names = ", ".join(t.split("·")[0].split("—")[0].strip()[:18] for t in seg.head(5).index)
    print(f"  {lbl}: {names}")

# ---- heatmap ----
data = roll3[order].T
fig, ax = plt.subplots(figsize=(16, 13), facecolor="black"); ax.set_facecolor("black")
im = ax.imshow(data.values, aspect="auto", cmap="RdYlGn", vmin=-20, vmax=20, interpolation="nearest")
ax.set_yticks(range(len(order)))
ax.set_yticklabels([t.split("·")[0].split("—")[0].strip()[:26] for t in order], fontsize=7, color="#ddd")
months = list(data.columns)
xt = [i for i, m in enumerate(months) if m.month in (1, 4, 7, 10)]
ax.set_xticks(xt)
ax.set_xticklabels([months[i].strftime("%b'%y") for i in xt], fontsize=8, color="#bbb", rotation=45, ha="right")
PEAKS = ["2023-07-19","2023-12-19","2024-02-12","2024-07-10","2024-11-13","2025-07-03","2026-05-14"]
for p in PEAKS:
    pm = pd.Timestamp(p).to_period("M").to_timestamp("M")
    if pm in months:
        ax.axvline(months.index(pm), color="cyan", lw=0.9, ls=":", alpha=0.8)
ax.set_title("Theme leadership vs SPY (3-mo RS), 2023->now  — sorted by peak month; cyan = x-ADR peaks", color="white")
cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01); cb.ax.tick_params(colors="#bbb")
cb.set_label("3-mo RS vs SPY (%)", color="#ddd")
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=130, bbox_inches="tight")
print(f"\nsaved: {OUT}")
