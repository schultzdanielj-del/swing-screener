"""Mark each theme's ACTIVE SPAN (start = took off, end = back to sleep), not just
a start. Start/end derived from RS-vs-SPY crossing its own trend. Validation run on
the 2025 cycle (known answers). Gantt of spans under the SPY context. ASCII output."""
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
OUT = REPO / "research" / "theme_spans_2025.png"

LEADIN = pd.Timestamp("2025-04-01")    # RS window start (small lead-in)
CYC_START, CYC_END = pd.Timestamp("2025-05-01"), pd.Timestamp("2025-10-31")
DISP_END = pd.Timestamp("2025-11-15")
ADR_N = 20
N_DOM = 16

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

clo = {t: series(t)["close"].astype(float).loc[LEADIN:DISP_END] for t in cache}
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

def span(comp_series):
    """start = first RS cross above its trend; end = first post-peak cross below a falling trend."""
    rs = (comp_series / SPYw).dropna()
    rs = rs.loc[LEADIN:DISP_END]
    r = rs.ewm(span=5, adjust=False).mean()
    tr = r.ewm(span=21, adjust=False).mean()
    v, t, idx = r.values, tr.values, r.index
    n = len(v)
    if n < 25: return None, None, None
    peak = int(np.argmax(v))
    start_i = next((i for i in range(1, max(2, peak + 1)) if v[i] > t[i] and v[i-1] <= t[i-1]), 0)
    end_i = None
    for i in range(peak + 1, n):
        if v[i] < t[i] and (t[i] - t[max(0, i-5)]) < 0:
            end_i = i; break
    return idx[start_i], (idx[end_i] if end_i is not None else None), idx[peak]

# rank themes by cycle RS, keep dominant
cand = []
for k, mem in THEMES.items():
    c = comp(mem)
    if c is None or pd.isna(asof(c, CYC_START)): continue
    r = (asof(c, CYC_END) / asof(c, CYC_START)) / (asof(SPYw, CYC_END) / asof(SPYw, CYC_START)) - 1
    if np.isnan(r): continue
    cand.append(dict(lbl=THEME_LABELS.get(k, k), c=c, rs=r))
cand.sort(key=lambda r: r["rs"], reverse=True)
dom = cand[:N_DOM]

spans = []
for r in dom:
    s, e, pk = span(r["c"])
    if s is None: continue
    spans.append(dict(lbl=r["lbl"], start=s, end=e, peak=pk, rs=r["rs"]))
spans.sort(key=lambda x: x["start"])

print("THEME ACTIVE SPANS -- 2025 (start = took off, end = back to sleep; 'open' = still awake at cycle end)")
print(f"{'start':12s} {'end':12s} {'peak':12s}  theme")
for s in spans:
    end = s["end"].date() if s["end"] is not None else "open"
    print(f"{str(s['start'].date()):12s} {str(end):12s} {str(s['peak'].date()):12s}  "
          f"{s['lbl'].encode('ascii','ignore').decode().strip()}")

# ---- plot: SPY context (top) + Gantt of spans (bottom) ----
pp = pd.DataFrame({"close": SPYc, "sma50": sma50, "ema21": ema21, "xadr": xadr}).loc[LEADIN:DISP_END]
fig, (a1, a2) = plt.subplots(2, 1, figsize=(15, 11), sharex=True, facecolor="black",
                             gridspec_kw={"height_ratios": [1.3, 2.0]})
for ax in (a1, a2):
    ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, axis="x", alpha=0.15)
a1.plot(pp.index, pp["close"], color="white", lw=1.1, label="SPY")
a1.plot(pp.index, pp["sma50"], color="#ffcc00", lw=0.9)
a1.plot(pp.index, pp["ema21"], color="#5fc8ff", lw=0.9)
a1.axvline(pd.Timestamp("2025-07-03"), color="#00ffd5", lw=1.0, ls=":")
a1.axvline(pd.Timestamp("2025-08-01"), color="#ffffff", lw=1.0, ls="--")
a1.set_title("Theme active spans 2025  (bar = awake; cap = went back to sleep; arrow = still awake)\n"
             "cyan=x-ADR peak 7/3, white=reset 8/1", color="white", fontsize=11)
for i, s in enumerate(spans):
    end = s["end"] if s["end"] is not None else DISP_END
    col = "#1eff1e" if s["start"] < pd.Timestamp("2025-08-01") else "#ff9500"
    a2.plot([s["start"], end], [i, i], color=col, lw=5, solid_capstyle="butt", alpha=0.85)
    a2.scatter([s["start"]], [i], color=col, s=45, zorder=5, marker="|")
    if s["end"] is not None:
        a2.scatter([s["end"]], [i], color="#ff3030", s=70, zorder=5, marker="|")
    else:
        a2.annotate("", xy=(DISP_END, i), xytext=(end - pd.Timedelta(days=6), i),
                    arrowprops=dict(arrowstyle="->", color=col))
    a2.text(s["start"] - pd.Timedelta(days=2), i, s["lbl"].split("·")[0].split("—")[0].strip()[:24] + " ",
            color="#ddd", fontsize=7.5, ha="right", va="center")
a2.axvline(pd.Timestamp("2025-07-03"), color="#00ffd5", lw=1.0, ls=":")
a2.axvline(pd.Timestamp("2025-08-01"), color="#ffffff", lw=1.0, ls="--")
a2.set_yticks([]); a2.set_ylim(-1, len(spans))
a2.set_xlim(pd.Timestamp("2025-04-20"), DISP_END)
a2.xaxis.set_major_locator(mdates.MonthLocator())
a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=135, bbox_inches="tight")
print(f"\nsaved plot: {OUT}")
