"""What happened to each theme ACROSS the end-July 2025 range-expansion reset.
Phase 1 = 5/01->8/01 (into the reset). Reset window ~7/28-8/11. Phase 2 = 8/01->10/31.
Rotation delta = phase-2 RS - phase-1 RS  (positive = reloaded in, negative = taken out).
Read-only. Composites auto-degrade to members with 2025 data; coverage reported."""
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
OUT = REPO / "research" / "theme_rotation_2025.png"

START, END = pd.Timestamp("2025-04-15"), pd.Timestamp("2025-11-15")
T_START = pd.Timestamp("2025-05-01")   # cycle start
T_RESET = pd.Timestamp("2025-08-01")   # range expansion
T_END = pd.Timestamp("2025-10-31")     # phase-2 end
RHI_A, RHI_B = pd.Timestamp("2025-07-15"), pd.Timestamp("2025-07-28")  # pre-reset high
RLO_A, RLO_B = pd.Timestamp("2025-07-28"), pd.Timestamp("2025-08-11")  # reset low

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print("tickers in cache:", len(cache))

clo = {}
for t, df in cache.items():
    d = df.copy(); d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float).sort_index()
    clo[t] = s[~s.index.duplicated(keep="last")].loc[START:END]
SPY = clo["SPY"]

def asof(s, dt):
    v = s.asof(dt)
    return v if pd.notna(v) else np.nan

spy_p1 = asof(SPY, T_RESET) / asof(SPY, T_START) - 1
spy_p2 = asof(SPY, T_END) / asof(SPY, T_RESET) - 1
print(f"SPY  phase1 {spy_p1*100:+.1f}%   phase2 {spy_p2*100:+.1f}%")

def composite(members):
    cols, cov = [], 0
    for m in members:
        if m in clo and clo[m].dropna().shape[0] >= 60:
            s = clo[m].dropna()
            fv = s.iloc[0]
            if fv > 0:
                cols.append(s / fv * 100.0); cov += 1
    if not cols:
        return None, 0
    wide = pd.concat(cols, axis=1)
    need = max(1, wide.shape[1] // 2)
    comp = wide.mean(axis=1, skipna=True).where(wide.notna().sum(axis=1) >= need)
    return comp.dropna(), cov

rows = []
for key, members in THEMES.items():
    comp, cov = composite(members)
    if comp is None or asof(comp, T_START) is None or np.isnan(asof(comp, T_START)) or asof(comp, T_START) <= 0:
        continue
    p1 = asof(comp, T_RESET) / asof(comp, T_START) - 1
    p2 = asof(comp, T_END) / asof(comp, T_RESET) - 1
    if np.isnan(p1) or np.isnan(p2):
        continue
    hi = comp.loc[RHI_A:RHI_B].max(); lo = comp.loc[RLO_A:RLO_B].min()
    reset_dd = (lo / hi - 1) if (pd.notna(hi) and pd.notna(lo) and hi > 0) else np.nan
    p1_rs, p2_rs = p1 - spy_p1, p2 - spy_p2
    rows.append(dict(key=key, lbl=THEME_LABELS.get(key, key), cov=cov, tot=len(members),
                     p1=p1, p2=p2, p1_rs=p1_rs, p2_rs=p2_rs, dd=reset_dd,
                     delta=p2_rs - p1_rs))

df = pd.DataFrame(rows)
df = df[df["cov"] >= 3].copy()
print(f"themes analyzed (>=3 members w/ 2025 data): {len(df)}\n")

def show(title, d, n=12):
    print("="*92); print(title); print("="*92)
    print(f"{'theme':30s} cov   P1rs%   P2rs%   ResetDD%   delta%")
    for _, r in d.head(n).iterrows():
        lc = "" if r["cov"] / r["tot"] >= 0.5 else " *low-cov"
        print(f"{r['lbl'][:30]:30s} {int(r['cov']):2d}/{int(r['tot']):<2d} "
              f"{r['p1_rs']*100:+6.1f}  {r['p2_rs']*100:+6.1f}   {r['dd']*100:+6.1f}    {r['delta']*100:+6.1f}{lc}")

show("PHASE-1 LEADERS (top RS into the reset)", df.sort_values("p1_rs", ascending=False))
print()
show("ROTATED IN  -> reloaded for phase 2 (most positive rotation delta)", df.sort_values("delta", ascending=False))
print()
show("ROTATED OUT -> profit-taken after the reset (most negative rotation delta)", df.sort_values("delta"))
print()
show("DEEPEST DRAWDOWN at the reset candle", df.sort_values("dd"))

# correlation: did phase-1 leaders get hit harder at the reset?
sub = df.dropna(subset=["dd"])
if len(sub) > 5:
    print(f"\ncorr(phase-1 RS, reset drawdown) = {np.corrcoef(sub['p1_rs'], sub['dd'])[0,1]:+.2f}  "
          "(negative => stronger phase-1 leaders fell harder at the reset)")

# ---- quadrant scatter ----
fig, ax = plt.subplots(figsize=(12, 10), facecolor="black"); ax.set_facecolor("black")
ax.axhline(0, color="#888", lw=0.8); ax.axvline(0, color="#888", lw=0.8)
for _, r in df.iterrows():
    x, y = r["p1_rs"]*100, r["p2_rs"]*100
    if x >= 0 and y >= 0:   col = "#1eff1e"   # persistent leader
    elif x < 0 and y >= 0:  col = "#5fc8ff"   # rotated IN
    elif x >= 0 and y < 0:  col = "#ff6666"   # rotated OUT
    else:                   col = "#888888"   # laggard
    ax.scatter([x], [y], s=60, color=col, edgecolor="white", linewidth=0.4, zorder=3)
    ax.text(x + 0.4, y, r["lbl"][:22], color="#ddd", fontsize=6.5, va="center")
ax.set_xlabel("Phase-1 RS vs SPY  (5/1 -> 8/1)  %", color="#ddd")
ax.set_ylabel("Phase-2 RS vs SPY  (8/1 -> 10/31)  %", color="#ddd")
ax.set_title("Theme rotation across the end-July 2025 reset\n"
             "top-left = reloaded IN | bottom-right = profit-taken OUT", color="white")
ax.tick_params(colors="#bbb"); ax.grid(True, alpha=0.15)
plt.tight_layout(); plt.savefig(OUT, facecolor="black", dpi=130, bbox_inches="tight")
print(f"\nsaved plot: {OUT}")
