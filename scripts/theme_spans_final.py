"""Theme spans: START = chord-elbow takeoff, END = highest close before the first
10/20 SMA bear cross after the peak (open if no cross yet). Renders 2025 + 2026. ASCII."""
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

def span(comp, leadin, cyc, disp):
    rs = (comp / SPY.reindex(comp.index).ffill()).dropna()
    rs_s = rs.ewm(span=5, adjust=False).mean()
    rs_peak = rs_s.loc[cyc:disp].idxmax()
    seg = np.log(rs_s.loc[leadin:rs_peak].values)
    start = rs_s.loc[leadin:rs_peak].index[chord_up(seg)] if len(seg) >= 6 else rs_s.loc[leadin:rs_peak].index[0]
    # end = highest close before first 10/20 bear cross after the price peak
    sma10, sma20 = comp.rolling(10).mean(), comp.rolling(20).mean()
    ptop = comp.loc[cyc:disp].idxmax()
    after = comp.loc[ptop:disp]
    cross = None
    for j in range(1, len(after)):
        d, dp = after.index[j], after.index[j-1]
        if sma10.loc[d] < sma20.loc[d] and sma10.loc[dp] >= sma20.loc[dp]:
            cross = d; break
    end = comp.loc[start:cross].idxmax() if cross is not None else None
    return start, end

def dom_themes(build, cyc, disp, n):
    out = []
    for k, mem in THEMES.items():
        c = composite(mem, build, disp)
        if c is None: continue
        rs = (c / SPY.reindex(c.index).ffill()).dropna()
        if rs.loc[cyc:].empty: continue
        a = rs.asof(pd.Timestamp(cyc))
        if pd.isna(a) or a <= 0: continue
        out.append((k, mem, c, rs.iloc[-1]/a - 1))
    out.sort(key=lambda r: r[3], reverse=True)
    return out[:n]

def render(build, leadin, cyc, disp, n, title, out, vlines):
    spans = []
    for k, mem, comp, _ in dom_themes(build, cyc, disp, n):
        s, e = span(comp, leadin, cyc, disp)
        spans.append(dict(lbl=THEME_LABELS.get(k, k), start=s, end=e))
    spans.sort(key=lambda x: x["start"])
    print(f"\n{title}")
    for sp in spans:
        end = sp["end"].date() if sp["end"] is not None else "open (still up)"
        print(f"   {str(sp['start'].date()):12s} -> {str(end):16s}  {sp['lbl'].encode('ascii','ignore').decode().strip()}")
    SPYc = SPY.loc[build:disp]
    sma50m = SPYc.rolling(50).mean(); ema21m = SPYc.ewm(span=21, adjust=False).mean()
    disp0 = pd.Timestamp(leadin) - pd.Timedelta(days=10)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(15, 11), sharex=True, facecolor="black",
                                 gridspec_kw={"height_ratios": [1.2, 2.1]})
    for ax in (a1, a2):
        ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, axis="x", alpha=0.15)
    a1.plot(SPYc.loc[disp0:].index, SPYc.loc[disp0:].values, color="white", lw=1.1)
    a1.plot(sma50m.loc[disp0:].index, sma50m.loc[disp0:].values, color="#ffcc00", lw=0.9)
    a1.plot(ema21m.loc[disp0:].index, ema21m.loc[disp0:].values, color="#5fc8ff", lw=0.9)
    a1.set_title(title, color="white", fontsize=11)
    for vd, vc, ls in vlines:
        for ax in (a1, a2): ax.axvline(pd.Timestamp(vd), color=vc, lw=1.0, ls=ls, alpha=0.8)
    dend = pd.Timestamp(disp)
    for i, sp in enumerate(spans):
        end = sp["end"] if sp["end"] is not None else dend
        col = "#1eff1e" if sp["end"] is not None else "#ffcc44"
        a2.plot([sp["start"], end], [i, i], color=col, lw=6, solid_capstyle="butt", alpha=0.85)
        a2.scatter([sp["start"]], [i], color="#66ff66", s=50, zorder=5, marker="|")
        if sp["end"] is not None:
            a2.scatter([sp["end"]], [i], color="#ff3030", s=90, zorder=5, marker="|")
        else:
            a2.annotate("", xy=(dend, i), xytext=(end - pd.Timedelta(days=5), i),
                        arrowprops=dict(arrowstyle="->", color=col))
        a2.text(sp["start"] - pd.Timedelta(days=2), i,
                sp["lbl"].split("·")[0].split("—")[0].strip()[:24] + " ",
                color="#ddd", fontsize=7.5, ha="right", va="center")
    a2.set_yticks([]); a2.set_ylim(-1, len(spans)); a2.set_xlim(disp0, dend)
    a2.xaxis.set_major_locator(mdates.MonthLocator())
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    a2.text(0.99, 0.02, "green start = took off   red cap = top (highest close before 10/20 bear cross)   arrow = still up",
            transform=a2.transAxes, color="#999", fontsize=8, ha="right")
    plt.tight_layout(); plt.savefig(out, facecolor="black", dpi=135, bbox_inches="tight")
    print(f"saved: {out}")

render("2025-01-01", "2025-04-01", "2025-05-01", "2025-11-15", 16,
       "Theme spans 2025 (start=takeoff, end=top before 10/20 bear cross)",
       REPO / "research" / "theme_spans_2025_final.png",
       [("2025-07-03", "#00ffd5", ":"), ("2025-08-01", "#ffffff", "--")])
render("2025-11-01", "2026-03-01", "2026-04-08", "2026-06-05", 14,
       "Theme spans 2026 (start=takeoff, end=top before 10/20 bear cross)",
       REPO / "research" / "theme_spans_2026_final.png",
       [("2026-04-08", "#66ff66", "--"), ("2026-05-14", "#00ffd5", ":")])
