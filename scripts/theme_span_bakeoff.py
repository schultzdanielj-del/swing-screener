"""Bake-off of theme 'back-to-sleep' END detectors. START is fixed = validated
chord-elbow on RS. Five END methods scored on 2025 by false-end rate, lag, and
jitter; the most robust is then rendered as start/end spans on 2025 + 2026. ASCII."""
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

SPY_ALL = series("SPY")
CLO_ALL = {t: series(t) for t in cache}

def composite(members, a, b):
    cols = []
    for m in members:
        s = CLO_ALL.get(m)
        if s is None: continue
        s = s.loc[a:b].dropna()
        if s.shape[0] >= 60 and s.iloc[0] > 0:
            cols.append(s / s.iloc[0] * 100)
    if not cols: return None
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna()

def slope(arr, i, k=5):
    return arr[i] - arr[max(0, i - k)]

def chord_up(y):
    n = len(y); x = np.arange(n)
    line = y[0] + (y[-1] - y[0]) * (x / (n - 1))
    return int(np.argmax(line - y))

def chord_dn(y):
    n = len(y); x = np.arange(n)
    line = y[0] + (y[-1] - y[0]) * (x / (n - 1))
    return int(np.argmax(y - line))

def build(members, build_start, disp_end, smooth):
    comp = composite(members, build_start, disp_end)
    if comp is None: return None
    spy = SPY_ALL.reindex(comp.index).ffill()
    rs = (comp / spy).dropna()
    comp = comp.reindex(rs.index)
    return dict(comp=comp, rs=rs,
                rs_s=rs.ewm(span=smooth, adjust=False).mean(),
                rs_tr=rs.ewm(span=21, adjust=False).mean(),
                sma50=comp.rolling(50).mean(),
                ema21=comp.ewm(span=21, adjust=False).mean(),
                sma10=comp.rolling(10).mean(),
                sma20=comp.rolling(20).mean())

def peak_start(ctx, leadin, cyc_start, disp_end):
    rs_s = ctx["rs_s"]
    cyc = rs_s.loc[cyc_start:disp_end]
    if len(cyc) < 5: return None, None
    peak = cyc.idxmax()
    seg = np.log(rs_s.loc[leadin:peak].values)
    if len(seg) < 6: return peak, rs_s.loc[leadin:peak].index[0]
    return peak, rs_s.loc[leadin:peak].index[chord_up(seg)]

def end_method(name, ctx, start, peak, disp_end):
    rs_s = ctx["rs_s"]; rs_tr = ctx["rs_tr"]; comp = ctx["comp"]; sma50 = ctx["sma50"]; ema21 = ctx["ema21"]
    idx = rs_s.loc[peak:disp_end].index
    if len(idx) < 3: return None
    rsv = rs_s.values; trv = rs_tr.values; cv = comp.values; s50 = sma50.values; e21 = ema21.values
    s10 = ctx["sma10"].values; s20 = ctx["sma20"].values
    posmap = {d: i for i, d in enumerate(rs_s.index)}
    pk = posmap[peak]
    for d in idx[1:]:
        i = posmap[d]
        if name == "A_RSloseTrend":
            if rsv[i] < trv[i] and slope(trv, i) < 0: return d
        elif name == "B_RS50retrace":
            leg = rsv[pk] - rsv[posmap[start]]
            if leg > 0 and rsv[i] <= rsv[pk] - 0.5 * leg: return d
        elif name == "C_priceLose50sma":
            if not np.isnan(s50[i]) and cv[i] < s50[i] and slope(s50, i) < 0: return d
        elif name == "D_priceLoseEma21":
            if cv[i] < e21[i] and slope(e21, i) < 0: return d
        elif name == "F_price1020bear":
            if not np.isnan(s10[i]) and not np.isnan(s20[i-1]) and s10[i] < s20[i] and s10[i-1] >= s20[i-1]:
                return d
        elif name == "E_RSchordDown":
            pass
    if name == "E_RSchordDown":
        seg = np.log(rs_s.loc[peak:disp_end].values)
        if rsv[posmap[disp_end if disp_end in posmap else idx[-1]]] < rsv[pk] * 0.95:
            return idx[chord_dn(seg)]
        return None
    return None

METHODS = ["A_RSloseTrend", "B_RS50retrace", "C_priceLose50sma", "D_priceLoseEma21", "E_RSchordDown", "F_price1020bear"]

# ---------- SELECTION on 2025 ----------
B25 = dict(build="2025-01-01", leadin="2025-04-01", cyc="2025-05-01", disp="2025-11-15")
def dom_themes(cfg, n=16):
    out = []
    for k, mem in THEMES.items():
        c = composite(mem, cfg["build"], cfg["disp"])
        if c is None: continue
        spy = SPY_ALL.reindex(c.index).ffill()
        rs = (c / spy).dropna()
        if rs.loc[cfg["cyc"]:].empty: continue
        a = rs.asof(pd.Timestamp(cfg["cyc"])); b = rs.iloc[-1]
        if pd.isna(a) or a <= 0: continue
        out.append((k, mem, b / a - 1))
    out.sort(key=lambda r: r[2], reverse=True)
    return [(k, mem) for k, mem, _ in out[:n]]

dom25 = dom_themes(B25)
LEADINS = ["2025-03-15", "2025-04-01"]; SMOOTHS = [4, 8]
results = {m: dict(ends=[], false=0, ncall=0, lags=[], jitters=[]) for m in METHODS}

for k, mem in dom25:
    base = build(mem, B25["build"], B25["disp"], 5)
    if base is None: continue
    peak, start = peak_start(base, B25["leadin"], B25["cyc"], B25["disp"])
    if peak is None: continue
    pkpos = {d: i for i, d in enumerate(base["rs"].index)}[peak]
    rs_raw = base["rs"].values; n = len(rs_raw)
    for m in METHODS:
        e = end_method(m, base, start, peak, pd.Timestamp(B25["disp"]))
        # jitter across grid
        gj = []
        for li in LEADINS:
            for sm in SMOOTHS:
                ctxg = build(mem, B25["build"], B25["disp"], sm)
                pkg, stg = peak_start(ctxg, li, B25["cyc"], B25["disp"])
                if pkg is None: continue
                eg = end_method(m, ctxg, stg, pkg, pd.Timestamp(B25["disp"]))
                if eg is not None: gj.append(eg.toordinal())
        if len(gj) >= 2: results[m]["jitters"].append(float(np.std(gj)))
        if e is None: continue
        results[m]["ncall"] += 1
        epos = {d: i for i, d in enumerate(base["rs"].index)}[e]
        results[m]["lags"].append(epos - pkpos)
        fpos = min(epos + 20, n - 1)
        fwd = rs_raw[fpos] / rs_raw[epos] - 1
        if fwd > 0.03: results[m]["false"] += 1  # re-led after being called asleep

print("END-METHOD BAKE-OFF on 2025  (16 dominant themes)")
print(f"{'method':18s} {'calls':>5s} {'false-end':>9s} {'medLag(td)':>10s} {'medJitter(d)':>12s}")
rank = []
for m in METHODS:
    r = results[m]
    fr = r["false"] / r["ncall"] if r["ncall"] else np.nan
    ml = np.median(r["lags"]) if r["lags"] else np.nan
    mj = np.median(r["jitters"]) if r["jitters"] else np.nan
    rank.append((m, fr, ml, mj, r["ncall"]))
    print(f"{m:18s} {r['ncall']:5d} {fr*100:8.0f}% {ml:10.0f} {mj:12.1f}")
rank = [x for x in rank if not np.isnan(x[1])]
rank.sort(key=lambda x: (x[1], x[3] if not np.isnan(x[3]) else 999))
winner = rank[0][0]
print(f"\n--> most robust end method: {winner}  (lowest false-end, then jitter)")

# ---------- RENDER winner on a cycle ----------
def render(cfg, title, out, vlines, n_dom=16):
    spd = series  # noqa
    SPYc = SPY_ALL.loc[cfg["build"]:cfg["disp"]]
    sma50m = SPYc.rolling(50).mean(); ema21m = SPYc.ewm(span=21, adjust=False).mean()
    sp = series_full = None
    spans = []
    for k, mem in dom_themes(cfg, n_dom):
        ctx = build(mem, cfg["build"], cfg["disp"], 5)
        if ctx is None: continue
        peak, start = peak_start(ctx, cfg["leadin"], cfg["cyc"], cfg["disp"])
        if peak is None: continue
        e = end_method(winner, ctx, start, peak, pd.Timestamp(cfg["disp"]))
        spans.append(dict(lbl=THEME_LABELS.get(k, k), start=start, end=e))
    spans.sort(key=lambda x: x["start"])
    disp0 = pd.Timestamp(cfg["leadin"]) - pd.Timedelta(days=10)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(15, 11), sharex=True, facecolor="black",
                                 gridspec_kw={"height_ratios": [1.2, 2.1]})
    for ax in (a1, a2):
        ax.set_facecolor("black"); ax.tick_params(colors="#bbb"); ax.grid(True, axis="x", alpha=0.15)
    sl = SPYc.loc[disp0:]
    a1.plot(sl.index, sl.values, color="white", lw=1.1)
    a1.plot(sma50m.loc[disp0:].index, sma50m.loc[disp0:].values, color="#ffcc00", lw=0.9)
    a1.plot(ema21m.loc[disp0:].index, ema21m.loc[disp0:].values, color="#5fc8ff", lw=0.9)
    a1.set_title(title, color="white", fontsize=11)
    for vd, vc, ls in vlines:
        for ax in (a1, a2):
            ax.axvline(pd.Timestamp(vd), color=vc, lw=1.0, ls=ls, alpha=0.8)
    cyc_end_disp = pd.Timestamp(cfg["disp"])
    for i, s in enumerate(spans):
        end = s["end"] if s["end"] is not None else cyc_end_disp
        col = "#1eff1e" if s["end"] is not None else "#ffcc44"
        a2.plot([s["start"], end], [i, i], color=col, lw=6, solid_capstyle="butt", alpha=0.85)
        a2.scatter([s["start"]], [i], color="#66ff66", s=50, zorder=5, marker="|")
        if s["end"] is not None:
            a2.scatter([s["end"]], [i], color="#ff3030", s=80, zorder=5, marker="|")
        else:
            a2.annotate("", xy=(cyc_end_disp, i), xytext=(end - pd.Timedelta(days=5), i),
                        arrowprops=dict(arrowstyle="->", color=col))
        a2.text(s["start"] - pd.Timedelta(days=2), i,
                s["lbl"].split("·")[0].split("—")[0].strip()[:24] + " ",
                color="#ddd", fontsize=7.5, ha="right", va="center")
    a2.set_yticks([]); a2.set_ylim(-1, len(spans)); a2.set_xlim(disp0, cyc_end_disp)
    a2.xaxis.set_major_locator(mdates.MonthLocator())
    a2.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
    a2.text(0.99, 0.02, "red cap = back to sleep   yellow/arrow = still awake", transform=a2.transAxes,
            color="#999", fontsize=8, ha="right")
    plt.tight_layout(); plt.savefig(out, facecolor="black", dpi=135, bbox_inches="tight")
    print(f"saved: {out}")
    for s in spans:
        end = s["end"].date() if s["end"] is not None else "open"
        print(f"   {str(s['start'].date()):12s} -> {str(end):12s}  {s['lbl'].encode('ascii','ignore').decode().strip()}")

print("\n=== 2025 validation spans ===")
render(B25, f"Theme spans 2025  (start=chord-elbow, end={winner})  cyan=x-ADR peak, white=reset",
       REPO / "research" / "theme_spans_2025_v2.png",
       [("2025-07-03", "#00ffd5", ":"), ("2025-08-01", "#ffffff", "--")])

B26 = dict(build="2025-11-01", leadin="2026-03-01", cyc="2026-04-08", disp="2026-06-05")
print("\n=== 2026 spans ===")
render(B26, f"Theme spans 2026 cycle  (start=chord-elbow, end={winner})  green=4/8 start, cyan=x-ADR peak 5/14",
       REPO / "research" / "theme_spans_2026.png",
       [("2026-04-08", "#66ff66", "--"), ("2026-05-14", "#00ffd5", ":")], n_dom=14)
