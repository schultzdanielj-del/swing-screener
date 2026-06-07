"""Relative-strength elbow ("came into play") for each of the 53 movers.
Runs 2 denominators x 2 detectors, scores robustness by elbow-date jitter under a
window/smoothing grid, keeps the most robust combo, and emits the elbow-ordered
theme-rotation timeline + one overview plot. Read-only over the OHLCV cache."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")
OUT_PNG = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\research\rs_elbow_timeline.png")

BUCKETS = {
    "A AI-semis/hw":   ["MRVL","QCOM","INTC","MU","GFS","NVTS","MXL","SMTC","AOSL","POET","CEVA","ACMR","SNDK","STX","VSH","VICR","NTAP","EXTR","DELL","HPE","SMCI"],
    "B Software/cyber":["FROG","DDOG","TENB","TWLO","TEAM","NAVN","RBRK","FTNT","GTLB","CRWD","BB"],
    "C Space/drones":  ["RKLB","RDW","UMAC","LUNR","SATL"],
    "D Crypto->AI DC": ["BTDR","DGXX","DXYZ"],
    "E Auton/sensing": ["AEVA","AUR"],
    "F Power/energy":  ["SEDG","TE","FCEL","SGML","GTX"],
    "G Healthcare":    ["OSCR","HUM","GH","HNGE"],
    "H Outliers":      ["CAR","SG"],
}
T53 = [t for b in BUCKETS.values() for t in b]
BUCKET_OF = {t: b for b, ts in BUCKETS.items() for t in ts}

END = "2026-06-05"
WIN_STARTS = ["2026-01-02", "2026-01-20", "2026-02-09"]  # robustness grid
SPANS = [3, 8]                                            # robustness grid
MIN_SEG = 8

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print("CACHE:", CACHE)
print("tickers in cache:", len(cache))
assert "SPY" in cache, "SPY missing"
missing = [t for t in T53 if t not in cache]
print(f"requested 53 | present {53-len(missing)} | missing {missing}")

# date-indexed close for everything we need
ser = {}
for t in T53 + ["SPY"]:
    if t not in cache:
        continue
    d = cache[t].copy()
    d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float).sort_index()
    ser[t] = s[~s.index.duplicated(keep="last")]
close_wide = pd.DataFrame(ser).sort_index()


def cohort_ew(start):
    coh = close_wide[[t for t in T53 if t in ser]].loc[start:END]
    base = coh.apply(lambda c: c.loc[c.first_valid_index()] if c.first_valid_index() is not None else np.nan)
    reb = coh / base
    return reb.mean(axis=1, skipna=True)   # equal-weight, rebased to 1 at each col's first bar


def make_logrs(t, denom_kind, start):
    num = close_wide[t].loc[start:END]
    denom = close_wide["SPY"].loc[start:END] if denom_kind == "SPY" else cohort_ew(start)
    pair = pd.concat([num, denom], axis=1).dropna()
    if len(pair) < 2 * MIN_SEG + 1:
        return None
    n0, d0 = pair.iloc[0, 0], pair.iloc[0, 1]
    rs = (pair.iloc[:, 0] / n0) / (pair.iloc[:, 1] / d0)
    return np.log(rs)


def piecewise(y):
    n = len(y); x = np.arange(n, dtype=float)
    cx = np.concatenate([[0], np.cumsum(x)]);   cy = np.concatenate([[0], np.cumsum(y)])
    cxy = np.concatenate([[0], np.cumsum(x*y)]); cxx = np.concatenate([[0], np.cumsum(x*x)])
    cyy = np.concatenate([[0], np.cumsum(y*y)])
    def seg(a, b):
        k = b - a + 1
        Sx = cx[b+1]-cx[a]; Sy = cy[b+1]-cy[a]; Sxy = cxy[b+1]-cxy[a]
        Sxx = cxx[b+1]-cxx[a]; Syy = cyy[b+1]-cyy[a]
        den = k*Sxx - Sx*Sx
        m = 0.0 if abs(den) < 1e-9 else (k*Sxy - Sx*Sy)/den
        b0 = (Sy - m*Sx)/k
        return (Syy - m*Sxy - b0*Sy), m
    best = None
    for t in range(MIN_SEG, n-MIN_SEG):
        sl, ml = seg(0, t); sr, mr = seg(t, n-1)
        if best is None or sl+sr < best[0]:
            best = (sl+sr, t, ml, mr)
    _, t, ml, mr = best
    return t, (mr > ml)


def chord(y):
    n = len(y); x = np.arange(n)
    line = y[0] + (y[-1]-y[0]) * (x/(n-1))
    return int(np.argmax(line - y)), (y[-1] > y[0])


def elbow_date(t, denom_kind, detector, start, span):
    rs = make_logrs(t, denom_kind, start)
    if rs is None:
        return None
    y = rs.ewm(span=span, adjust=False).mean().values
    idx, up = piecewise(y) if detector == "pw" else chord(y)
    return rs.index[idx], up, float(rs.values[-1] - rs.values[0])


COMBOS = [("SPY", "pw"), ("SPY", "chord"), ("COH", "pw"), ("COH", "chord")]
COMBO_LBL = {("SPY","pw"):"SPY x hockey-stick", ("SPY","chord"):"SPY x chord",
             ("COH","pw"):"Cohort x hockey-stick", ("COH","chord"):"Cohort x chord"}

# grid run -> per (combo,ticker): list of elbow ordinals, up votes, net rs
grid = {c: {} for c in COMBOS}
for c in COMBOS:
    dk, det = c
    for t in T53:
        if t not in ser:
            continue
        ords, ups, nets = [], [], []
        for st in WIN_STARTS:
            for sp in SPANS:
                r = elbow_date(t, dk, det, st, sp)
                if r is None:
                    continue
                dt, up, net = r
                ords.append(dt.toordinal()); ups.append(up); nets.append(net)
        if ords:
            grid[c][t] = dict(med=int(np.median(ords)),
                              jitter=float(np.std(ords)),
                              up=(np.mean(ups) >= 0.5),
                              net=float(np.median(nets)))

print("\n" + "="*70)
print("COMBO ROBUSTNESS  (lower median jitter = more stable elbow)")
print("="*70)
rob = []
for c in COMBOS:
    js = [g["jitter"] for g in grid[c].values()]
    rob.append((c, np.median(js), np.mean(js), len(js)))
rob.sort(key=lambda r: r[1])
for c, mj, aj, n in rob:
    print(f"  {COMBO_LBL[c]:24s}  median jitter {mj:5.1f} d   mean {aj:5.1f} d   ({n} tickers)")
winner = rob[0][0]
print(f"\n  --> most robust: {COMBO_LBL[winner]}")

# cross-combo agreement per ticker (spread of the 4 combo medians, in days)
print("\ncross-combo agreement: median spread across the 4 combos =", end=" ")
spreads = []
for t in T53:
    meds = [grid[c][t]["med"] for c in COMBOS if t in grid[c]]
    if len(meds) >= 2:
        spreads.append(max(meds) - min(meds))
print(f"{np.median(spreads):.0f} d  (high-spread tickers are ambiguous)")

# winner timeline
w = grid[winner]
print("\n" + "="*70)
print(f"RS ELBOW per ticker  [{COMBO_LBL[winner]}]  (date came into play)")
print("="*70)
rows = []
for t in T53:
    if t not in w:
        continue
    g = w[t]
    rows.append((t, BUCKET_OF[t], pd.Timestamp.fromordinal(g["med"]), g["up"], g["net"], g["jitter"]))
for t, bk, dt, up, net, jit in sorted(rows, key=lambda r: r[2]):
    lead = "" if (up and net > 0) else "  (no up-elbow / RS laggard)"
    print(f"  {dt.date()}  {t:6s} {bk:16s} netRS {net:+.2f}  jit {jit:4.1f}d{lead}")

print("\n" + "="*70)
print("THEME ROTATION ORDER  (median RS-elbow per bucket, earliest first)")
print("="*70)
bsum = []
for b in BUCKETS:
    ds = [w[t]["med"] for t in BUCKETS[b] if t in w and w[t]["up"] and w[t]["net"] > 0]
    if ds:
        bsum.append((b, int(np.median(ds)), len(ds), len(BUCKETS[b])))
for b, med, n, tot in sorted(bsum, key=lambda r: r[1]):
    print(f"  {pd.Timestamp.fromordinal(med).date()}   {b:16s} ({n}/{tot} led)")

# ---- plot ----
colors = {"A AI-semis/hw":"#5fc8ff","B Software/cyber":"#1eff1e","C Space/drones":"#ff5fff",
          "D Crypto->AI DC":"#ffcc00","E Auton/sensing":"#ff8c00","F Power/energy":"#ff3030",
          "G Healthcare":"#9b8cff","H Outliers":"#888888"}
fig, ax = plt.subplots(figsize=(12, 11), facecolor="black")
ax.set_facecolor("black")
yi = 0; yticks = []; ylabels = []
order = [t for b in BUCKETS for t in sorted([x for x in BUCKETS[b] if x in w], key=lambda x: w[x]["med"])]
for t in order:
    g = w[t]; bk = BUCKET_OF[t]
    dt = pd.Timestamp.fromordinal(g["med"])
    faded = not (g["up"] and g["net"] > 0)
    ax.scatter([dt], [yi], s=70, color=colors[bk], alpha=0.35 if faded else 1.0,
               edgecolor="white", linewidth=0.4, zorder=3)
    ax.text(dt + pd.Timedelta(days=1.5), yi, t, color="#ddd", fontsize=7, va="center")
    yticks.append(yi); ylabels.append(""); yi += 1
for d, lbl, col in [("2026-03-30","SPY low","#ff6666"), ("2026-04-08","SPY cycle start (gap held)","#66ff66")]:
    ax.axvline(pd.Timestamp(d), color=col, lw=1.0, ls="--", alpha=0.8)
    ax.text(pd.Timestamp(d), yi+0.5, lbl, color=col, fontsize=8, rotation=90, va="bottom", ha="right")
handles = [plt.Line2D([0],[0], marker="o", ls="", mfc=colors[b], mec="white", label=b) for b in BUCKETS]
ax.legend(handles=handles, loc="lower right", fontsize=8, facecolor="#111", edgecolor="#444", labelcolor="#ddd")
ax.set_yticks([]); ax.set_title(f"RS elbow — when each name came into play  [{COMBO_LBL[winner]}]", color="white")
ax.xaxis.set_major_locator(mdates.MonthLocator())
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
ax.tick_params(colors="#bbb"); ax.grid(True, axis="x", alpha=0.2)
plt.tight_layout()
plt.savefig(OUT_PNG, facecolor="black", dpi=130, bbox_inches="tight")
print(f"\nsaved plot: {OUT_PNG}")
