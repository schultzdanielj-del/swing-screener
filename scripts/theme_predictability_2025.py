"""Can you tell, at/around the end-July 2025 reset, which themes lead phase 2?
Test several candidate predictors of phase-2 RS via cross-theme correlation
(Pearson + Spearman) and top-N persistence hit-rates. Read-only."""
import sys, pickle
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
sys.path.insert(0, str(REPO / "local_runner"))
from theme_map import THEMES, THEME_LABELS
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

START, END = pd.Timestamp("2025-04-15"), pd.Timestamp("2025-11-15")
D = {k: pd.Timestamp(v) for k, v in dict(
    start="2025-05-01", reset="2025-08-01", lp1="2025-07-01",
    ep2="2025-08-22", end="2025-10-31").items()}
RHI = (pd.Timestamp("2025-07-15"), pd.Timestamp("2025-07-28"))
RLO = (pd.Timestamp("2025-07-28"), pd.Timestamp("2025-08-11"))

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
clo = {}
for t, df in cache.items():
    d = df.copy(); d["date"] = pd.to_datetime(d["date"])
    s = d.set_index("date")["close"].astype(float).sort_index()
    clo[t] = s[~s.index.duplicated(keep="last")].loc[START:END]
SPY = clo["SPY"]

def asof(s, dt):
    v = s.asof(dt); return v if pd.notna(v) else np.nan

def comp(members):
    cols = []
    for m in members:
        if m in clo and clo[m].dropna().shape[0] >= 60 and clo[m].dropna().iloc[0] > 0:
            s = clo[m].dropna(); cols.append(s / s.iloc[0] * 100)
    if not cols: return None, 0
    w = pd.concat(cols, axis=1); need = max(1, w.shape[1] // 2)
    return w.mean(axis=1, skipna=True).where(w.notna().sum(axis=1) >= need).dropna(), len(cols)

def rs(series, a, b):
    return (asof(series, b) / asof(series, a)) / (asof(SPY, b) / asof(SPY, a)) - 1

rows = []
for k, mem in THEMES.items():
    c, cov = comp(mem)
    if c is None or cov < 3 or pd.isna(asof(c, D["start"])): continue
    hi = c.loc[RHI[0]:RHI[1]].max(); lo = c.loc[RLO[0]:RLO[1]].min()
    dd = (lo/hi - 1) if (pd.notna(hi) and pd.notna(lo) and hi > 0) else np.nan
    rows.append(dict(
        lbl=THEME_LABELS.get(k, k)[:26],
        p1=rs(c, D["start"], D["reset"]),
        latep1=rs(c, D["lp1"], D["reset"]),
        p2=rs(c, D["reset"], D["end"]),
        ep2=rs(c, D["reset"], D["ep2"]),
        rp2=rs(c, D["ep2"], D["end"]),
        dd=dd))
df = pd.DataFrame(rows).dropna(subset=["p1", "p2", "ep2", "rp2"])
print(f"themes: {len(df)}\n")

def corr(a, b):
    m = df[[a, b]].dropna()
    pear = np.corrcoef(m[a], m[b])[0, 1]
    spear = np.corrcoef(m[a].rank(), m[b].rank())[0, 1]
    return pear, spear

print("PREDICTORS OF PHASE-2 RS  (correlation across themes)")
print(f"{'predictor':42s}  Pearson  Spearman")
for lbl, a, b in [
    ("phase-1 RS  ->  phase-2 RS (can you tell early?)", "p1", "p2"),
    ("late-phase-1 RS (Jul run-in)  ->  phase-2 RS", "latep1", "p2"),
    ("reset drawdown  ->  phase-2 RS", "dd", "p2"),
    ("EARLY phase-2 RS (first 3wk post-reset) -> REST of phase-2 RS", "ep2", "rp2"),
]:
    p, s = corr(a, b)
    print(f"{lbl:42s}  {p:+5.2f}    {s:+5.2f}")

def hit(pre, post, n=8):
    top_pre = set(df.sort_values(pre, ascending=False).head(n)["lbl"])
    top_post = set(df.sort_values(post, ascending=False).head(n)["lbl"])
    return len(top_pre & top_post), n

h1 = hit("p1", "p2"); h2 = hit("ep2", "rp2")
print(f"\nPERSISTENCE of top-8 leaders:")
print(f"  phase-1 top-8  still in phase-2 top-8:     {h1[0]}/8")
print(f"  early-phase-2 top-8 still in rest-phase-2 top-8: {h2[0]}/8")

print("\nTop-8 phase-1 leaders -> their phase-2 rank (1=best of {} themes):".format(len(df)))
df["r_p1"] = df["p1"].rank(ascending=False); df["r_p2"] = df["p2"].rank(ascending=False)
df["r_ep2"] = df["ep2"].rank(ascending=False); df["r_rp2"] = df["rp2"].rank(ascending=False)
for _, r in df.sort_values("p1", ascending=False).head(8).iterrows():
    print(f"  {r['lbl']:26s}  P1 #{int(r['r_p1']):2d}  ->  P2 #{int(r['r_p2']):2d}")
print("\nTop-8 early-phase-2 (post-reset recovery) -> their rest-of-phase-2 rank:")
for _, r in df.sort_values("ep2", ascending=False).head(8).iterrows():
    print(f"  {r['lbl']:26s}  earlyP2 #{int(r['r_ep2']):2d}  ->  restP2 #{int(r['r_rp2']):2d}")
