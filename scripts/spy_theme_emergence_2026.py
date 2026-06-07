"""Same overlay as 2025, for the CURRENT cycle (start 4/8/2026 -> 6/5/2026).
Marks where dominant themes took off (RS elbow vs SPY within the cycle) and shows
the x-ADR panel so we can read where the cycle sits. ASCII-only console. Read-only."""
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
OUT = REPO / "research" / "spy_theme_emergence_2026.png"

DISP_START = pd.Timestamp("2026-02-01")     # chart lead-in (shows Feb/Mar reset)
T_START = pd.Timestamp("2026-04-08")        # cycle start
END = pd.Timestamp("2026-06-05")
EARLY_CUT = pd.Timestamp("2026-05-01")      # April take-off = early/green, May+ = orange
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
    clo[t] = series(t)["close"].astype(float).loc[DISP_START:END]
SPYw = clo["SPY"]

def asof(s, dt):
    v = s.asof(dt); return v if pd.notna(v) else np.nan

def comp(members):
    cols = []
    for m in members:
        if m in clo and clo[m].dropna().shape[0] >= 40 and clo[m].dropna().iloc[0] > 0:
            s = clo[m].dropna(); cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

def rs_ret(c, a, b):
    return (asof(c, b) / asof(c, a)) / (asof(SPYw, b) / asof(SPYw, a)) - 1

def chord_elbow(logrs):
    y = logrs.ewm(span=4, adjust=False).mean().values
    n = len(y)
    if n < 8: return None
    x = np.arange(n)
    line = y[0] + (y[-1] - y[0]) * (x / (n - 1))
    return logrs.index[int(np.argmax(line - y))]

# rank themes by RS gain over the cycle, keep the leaders
cand = []
for k, mem in THEMES.items():
    c = comp(mem)
    if c is None or pd.isna(asof(c, T_START)): continue
    r = rs_ret(c, T_START, END)
    if np.isnan(r): continue
    cand.append(dict(lbl=THEME_LABELS.get(k, k), c=c, rs=r))
cand.sort(key=lambda r: r["rs"], reverse=True)
dom = cand[:N_DOMINANT]

marks = []
for r in dom:
    logrs = np.log(((r["c"] / asof(r["c"], T_START)) / (SPYw / asof(SPYw, T_START))).loc[T_START:END].dropna())
    e = chord_elbow(logrs)
    if e is None: continue
    marks.append(dict(lbl=r["lbl"], date=e, rs=r["rs"]))
marks.sort(key=lambda m: m["date"])

# x-ADR read of the cycle
cyc_x = xadr.loc[T_START:END]
pk = cyc_x.idxmax()
cur = cyc_x.iloc[-1]
post_pk = cyc_x.loc[pk:]
lower_high = post_pk.max() < cyc_x.max() - 1e-9 and (post_pk.idxmax() != pk)
print(f"x-ADR cycle peak: {pk.date()}  {cyc_x.max():.2f} ADR")
print(f"x-ADR now ({cyc_x.index[-1].date()}): {cur:.2f} ADR")
print(f"trading days since peak: {len(post_pk)-1}")
print("STATUS: " + ("past the peak -- watch for lower high / reset" if (cyc_x.index[-1]-pk).days > 3
                     else "at/near the x-ADR peak -- still thrusting"))
print("\nDOMINANT THEME TAKE-OFFS this cycle (RS elbow vs SPY):")
print(f"{'date':12s} {'cycRS%':>7s}  theme")
for m in marks:
    print(f"{str(m['date'].date()):12s} {m['rs']*100:+7.0f}  {m['lbl'].encode('ascii','ignore').decode().strip()}")

# ---- plot ----
pp = pd.DataFrame({"close": SPYc, "sma50": sma50, "ema21": ema21, "xadr": xadr}).loc[DISP_START:END]
fig, (a1, a2) = plt.subplots(2, 1, figsize=(15, 10), sharex=True, facecolor="black",
                             gridspec_kw={"height_ratios": [2.7, 1.0]})
for ax in (a1, a2):
    ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.15)
a1.plot(pp.index, pp["close"], color="white", lw=1.2, label="SPY")
a1.plot(pp.index, pp["sma50"], color="#ffcc00", lw=1.0, label="50 SMA")
a1.plot(pp.index, pp["ema21"], color="#5fc8ff", lw=1.0, label="21 EMA")
a1.axvspan(T_START, END, color="#1eff1e", alpha=0.05)
a1.axvline(T_START, color="#66ff66", lw=1.0, ls="--", alpha=0.8)
pmin, pmax = pp["close"].min(), pp["close"].max()

def shortlbl(s): return s.split("·")[0].split("—")[0].strip()[:20]
clusters = []
for m in marks:
    if clusters and (m["date"] - clusters[-1][-1]["date"]).days <= 5:
        clusters[-1].append(m)
    else:
        clusters.append([m])
hcnt = 0
for cl in clusters:
    colc = "#1eff1e" if cl[0]["date"] < EARLY_CUT else "#ff9500"
    for m in cl:
        a1.scatter([m["date"]], [asof(SPYc, m["date"])], s=55, color=colc,
                   edgecolor="white", linewidth=0.5, zorder=5)
    if len(cl) <= 2:
        for m in cl:
            x = m["date"]; yd = asof(SPYc, x)
            yl = pmax * (1.004 + 0.020 * (hcnt % 6)); hcnt += 1
            a1.plot([x, x], [yd, yl], color=colc, lw=0.6, alpha=0.55)
            a1.text(x, yl, " " + shortlbl(m["lbl"]), color=colc, fontsize=7.5,
                    rotation=90, va="bottom", ha="center")
    else:
        cx = cl[len(cl) // 2]["date"]
        names = (cl[0]["date"].strftime("%b %d") + "-" + cl[-1]["date"].strftime("%d") + "\n"
                 + "\n".join(shortlbl(m["lbl"]) for m in cl))
        yt = pmax * 1.02
        a1.plot([cx, cx], [asof(SPYc, cx), yt], color=colc, lw=0.7, alpha=0.6)
        a1.text(cx, yt, names, color=colc, fontsize=7.5, va="bottom", ha="center",
                bbox=dict(boxstyle="round", facecolor="#1a1a1a", edgecolor=colc, alpha=0.92))
a1.legend(loc="lower right", fontsize=8, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
a1.set_ylim(pmin * 0.99, pmax * 1.30)
a1.set_title("SPY 2026 cycle (start 4/8) -- where themes took off so far  (green = April, orange = May+)", color="white")
col2 = np.where(pp["xadr"] >= 0, "#1eff1e", "#ff3030")
a2.bar(pp.index, pp["xadr"], color=col2, width=1.0, alpha=0.8)
a2.axhline(0, color="#888", lw=0.7)
a2.axvline(T_START, color="#66ff66", lw=1.0, ls="--", alpha=0.8)
a2.axvline(pk, color="#00ffd5", lw=1.0, ls=":")
a2.set_ylabel("x ADR to 50SMA", color="#ddd")
a2.xaxis.set_major_locator(mdates.MonthLocator())
a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=135, bbox_inches="tight")
print(f"\nsaved plot: {OUT}")
