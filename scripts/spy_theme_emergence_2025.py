"""Overlay on the SPY 2025 chart the points where dominant themes took off.
Each theme is marked at its RS-elbow (vs SPY) WITHIN its dominant phase:
 - phase-1-dominant themes -> their May-Jul launch
 - phase-2-dominant themes -> their post-reset / Sept second-wind emergence
Top + x-ADR panel. ASCII-only console output. Read-only."""
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
OUT = REPO / "research" / "spy_theme_emergence_2025.png"

START, END = pd.Timestamp("2025-04-15"), pd.Timestamp("2025-11-15")
T_START, T_RESET, T_END = pd.Timestamp("2025-05-01"), pd.Timestamp("2025-08-01"), pd.Timestamp("2025-10-31")
ADR_N = 20
N_DOMINANT = 14

with open(CACHE, "rb") as f:
    cache = pickle.load(f)

def series(tk):
    d = cache[tk].copy(); d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    return d[~d.index.duplicated(keep="last")]

spd = series("SPY")
SPYc = spd["close"].astype(float)
sma50 = SPYc.rolling(50).mean(); ema21 = SPYc.ewm(span=21, adjust=False).mean()
adr_pct = 100 * ((spd["high"].astype(float) / spd["low"].astype(float)).rolling(ADR_N).mean() - 1)
xadr = ((SPYc / sma50 - 1) * 100) / adr_pct

clo = {}
for t, df in cache.items():
    s = series(t)["close"].astype(float).loc[START:END]
    clo[t] = s
SPYw = clo["SPY"]

def asof(s, dt):
    v = s.asof(dt); return v if pd.notna(v) else np.nan

def comp(members):
    cols = []
    for m in members:
        if m in clo and clo[m].dropna().shape[0] >= 60 and clo[m].dropna().iloc[0] > 0:
            s = clo[m].dropna(); cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

def rs_ret(c, a, b):
    return (asof(c, b) / asof(c, a)) / (asof(SPYw, b) / asof(SPYw, a)) - 1

def chord_elbow(logrs):
    y = logrs.ewm(span=5, adjust=False).mean().values
    n = len(y)
    if n < 10: return None
    x = np.arange(n)
    line = y[0] + (y[-1] - y[0]) * (x / (n - 1))
    return logrs.index[int(np.argmax(line - y))]

# score every theme, keep dominant ones
cand = []
for k, mem in THEMES.items():
    c = comp(mem)
    if c is None or pd.isna(asof(c, T_START)): continue
    p1, p2 = rs_ret(c, T_START, T_RESET), rs_ret(c, T_RESET, T_END)
    if np.isnan(p1) or np.isnan(p2): continue
    cand.append(dict(k=k, lbl=THEME_LABELS.get(k, k), c=c, p1=p1, p2=p2, dom=max(p1, p2)))

cand.sort(key=lambda r: r["dom"], reverse=True)
dom = cand[:N_DOMINANT]

marks = []
for r in dom:
    phase2 = r["p2"] > r["p1"]
    a, b = (T_RESET, T_END) if phase2 else (T_START, T_RESET)
    logrs = np.log(((r["c"] / asof(r["c"], a)) / (SPYw / asof(SPYw, a))).loc[a:b].dropna())
    elbow = chord_elbow(logrs)
    if elbow is None: continue
    marks.append(dict(lbl=r["lbl"], date=elbow, phase=2 if phase2 else 1, p1=r["p1"], p2=r["p2"]))

marks.sort(key=lambda m: m["date"])
print("DOMINANT THEME TAKE-OFFS (RS elbow within dominant phase)")
print(f"{'date':12s} {'phase':6s} {'P1rs%':>7s} {'P2rs%':>7s}  theme")
for m in marks:
    lbl = m["lbl"].encode("ascii", "ignore").decode().strip()
    print(f"{str(m['date'].date()):12s} P{m['phase']:<5d} {m['p1']*100:+7.0f} {m['p2']*100:+7.0f}  {lbl}")

# ---- plot ----
pp = pd.DataFrame({"close": SPYc, "sma50": sma50, "ema21": ema21, "xadr": xadr}).loc[START:END]
fig, (a1, a2) = plt.subplots(2, 1, figsize=(15, 10), sharex=True, facecolor="black",
                             gridspec_kw={"height_ratios": [2.7, 1.0]})
for ax in (a1, a2):
    ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.15)
a1.plot(pp.index, pp["close"], color="white", lw=1.2, label="SPY")
a1.plot(pp.index, pp["sma50"], color="#ffcc00", lw=1.0, label="50 SMA")
a1.plot(pp.index, pp["ema21"], color="#5fc8ff", lw=1.0, label="21 EMA")
a1.axvspan(T_START, T_RESET, color="#1eff1e", alpha=0.06)
a1.axvspan(T_RESET, pd.Timestamp("2025-11-01"), color="#ffaa00", alpha=0.06)
pmin, pmax = pp["close"].min(), pp["close"].max()
def shortlbl(s):
    return s.split("·")[0].split("—")[0].strip()[:20]
# group take-offs into temporal clusters (<=6 calendar days apart)
clusters = []
for m in marks:
    if clusters and (m["date"] - clusters[-1][-1]["date"]).days <= 6:
        clusters[-1].append(m)
    else:
        clusters.append([m])
hcnt = 0
for cl in clusters:
    colc = "#1eff1e" if cl[0]["phase"] == 1 else "#ff9500"
    for m in cl:
        a1.scatter([m["date"]], [asof(SPYc, m["date"])], s=55, color=colc,
                   edgecolor="white", linewidth=0.5, zorder=5)
    if len(cl) <= 2:
        for m in cl:
            x = m["date"]; ydot = asof(SPYc, x)
            ylab = pmax * (1.005 + 0.022 * (hcnt % 6)); hcnt += 1
            a1.plot([x, x], [ydot, ylab], color=colc, lw=0.6, alpha=0.55)
            a1.text(x, ylab, " " + shortlbl(m["lbl"]), color=colc, fontsize=7.5,
                    rotation=90, va="bottom", ha="center")
    else:
        cx = cl[len(cl) // 2]["date"]
        title = "2nd wind " + cl[0]["date"].strftime("%b %d") + "-" + cl[-1]["date"].strftime("%d")
        names = title + "\n" + "\n".join(shortlbl(m["lbl"]) for m in cl)
        ytop = pmax * 1.03
        a1.plot([cx, cx], [asof(SPYc, cx), ytop], color=colc, lw=0.7, alpha=0.6)
        a1.text(cx, ytop, names, color=colc, fontsize=7.5, va="bottom", ha="center",
                bbox=dict(boxstyle="round", facecolor="#1a1a1a", edgecolor=colc, alpha=0.92))
a1.legend(loc="lower right", fontsize=8, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
a1.set_ylim(pmin * 0.985, pmax * 1.34)
a1.set_title("SPY 2025 -- where dominant themes took off  (green = phase-1 launch, orange = phase-2 / second-wind)", color="white")
col2 = np.where(pp["xadr"] >= 0, "#1eff1e", "#ff3030")
a2.bar(pp.index, pp["xadr"], color=col2, width=1.0, alpha=0.8)
a2.axhline(0, color="#888", lw=0.7)
for d, c in [("2025-07-03", "#00ffd5"), ("2025-08-01", "#ffffff"), ("2025-09-22", "#ffaa00")]:
    a2.axvline(pd.Timestamp(d), color=c, lw=1.0, ls="--", alpha=0.8)
a2.set_ylabel("x ADR to 50SMA", color="#ddd")
a2.xaxis.set_major_locator(mdates.MonthLocator())
a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=135, bbox_inches="tight")
print(f"\nsaved plot: {OUT}")
