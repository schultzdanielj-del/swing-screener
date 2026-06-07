"""Sizing probe for the Extension Peek backtest (2020->now).

Read-only. Loads OHLCV + market caches, computes the cheap entry gates on a
deterministic ticker sample, counts how many bars survive them, and times the
faithful per-bar cascade (adr20 ext + strict break re-filter). Projects the
full-universe runtime. Prints aggregate numbers only -- no files written.
"""
import os, sys, time, pickle
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner"))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")

from scripts.reversal_profile import compute_all_reversal_profile_series
from scripts.ext50_trendlines import cascade_at, _has_line_break

EXT_CAP = 4.0          # close <= 4 ADR above SMA50
CANDLE_CAP = 1.1       # signal candle range < 1.1 * ADR20
START = "2020-01-02"

def sma(a, n):
    return pd.Series(a).rolling(n).mean().values

def adr20(h, l):
    r = (h / np.where(l > 0, l, np.nan) - 1.0) * 100.0
    return pd.Series(r).rolling(20).mean().values

def ext50_adr20(c, h, l):
    s50 = sma(c, 50)
    a = adr20(h, l)
    pct = (c - s50) / np.where(s50 > 0, s50, np.nan) * 100.0
    return pct / np.where(a > 0, a, np.nan), s50, a

print("cache:", CACHE)
with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
    U = pickle.load(f)
print("universe tickers:", len(U))
assert len(U) > 11200, "ticker count too low -- STOP"
with open(os.path.join(CACHE, "market_ohlcv.pkl"), "rb") as f:
    M = pickle.load(f)

# ---- SPY/VIX regime gate -> date->bool (2020+) ----
spy = M["SPY"].copy()
spy["d"] = spy["date"].astype(str).str[:10]
c = spy["close"].values.astype(float)
s10, s20 = sma(c, 10), sma(c, 20)
rising10 = np.concatenate([[False]*3, s10[3:] > s10[:-3]])
rising20 = np.concatenate([[False]*3, s20[3:] > s20[:-3]])
spy_ok = (s10 > s20) & rising10 & rising20
spy_gate = {d: bool(x) for d, x in zip(spy["d"].values, spy_ok)}
vix = M["VIX.INDX"].copy()
vix["d"] = vix["date"].astype(str).str[:10]
vix_gate = {d: float(v) < 20.0 for d, v in zip(vix["d"].values, vix["close"].values)}
regime = {d: spy_gate.get(d, False) and vix_gate.get(d, False)
          for d in spy_gate if d >= START}
on = sum(1 for d, v in regime.items() if v)
print(f"regime days 2020+: {on} ON / {len(regime)} total ({100*on/max(len(regime),1):.0f}% on)")

# ---- deterministic sample: tickers with data back to START, >=300 bars ----
elig = []
for t in sorted(U.keys()):
    df = U[t]
    d0 = str(df["date"].iloc[0])[:10]
    if d0 <= START and len(df) >= 300:
        elig.append(t)
sample = elig[::max(1, len(elig)//40)][:40]
print(f"eligible (data back to {START}): {len(elig)}; sampling {len(sample)}")

surv_counts, rev_times, casc_times, casc_n = [], [], [], 0
for t in sample:
    df = U[t]
    d = df["date"].astype(str).str[:10].values
    c = df["close"].values.astype(float)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    ext, s50, a = ext50_adr20(c, h, l)
    s10, s20, s40, s200 = sma(c,10), sma(c,20), sma(c,40), sma(c,200)
    crange = (h / np.where(l>0,l,np.nan) - 1.0) * 100.0
    n = len(c)
    surv = []
    for i in range(n):
        if d[i] < START: continue
        if not regime.get(d[i], False): continue
        if np.isnan(s200[i]) or np.isnan(a[i]) or np.isnan(ext[i]): continue
        if not (c[i] > s200[i]): continue
        if not (s50[i] > s200[i]): continue
        if not (s10[i] > s20[i]): continue
        if not (s10[i] > s50[i] and s20[i] > s50[i]): continue
        if not (crange[i] < CANDLE_CAP * a[i]): continue
        if not (ext[i] < EXT_CAP): continue
        surv.append(i)
    surv_counts.append(len(surv))
    if surv:
        t0 = time.time()
        rp = compute_all_reversal_profile_series(ext)
        rev_times.append(time.time() - t0)
        levels = {k: rp.get(k) for k in ("upside_1","upside_2","downside_1","downside_2","chop_upper")}
        for i in surv[:25]:
            la = {k: (float(levels[k][i]) if levels[k] is not None and not np.isnan(levels[k][i]) else float("nan")) for k in levels}
            t0 = time.time()
            snap = cascade_at(ext, i, la)
            # strict re-filter (same as snapshot builder)
            for cc in (snap.get("all_candidates") or []):
                if cc["anchor_type"] == "peak_anchored":
                    _has_line_break(ext, cc["i0"], cc["v0"], cc["i1"], cc["v1"], i)
            casc_times.append(time.time() - t0)
            casc_n += 1

sc = np.array(surv_counts)
print(f"\nsurvivor bars/ticker: mean={sc.mean():.1f} median={np.median(sc):.0f} max={sc.max()} | tickers with >=1: {(sc>0).sum()}/{len(sc)}")
print(f"reversal_profile pass: mean={np.mean(rev_times):.2f}s/ticker (n={len(rev_times)})")
print(f"cascade_at+filter: mean={1000*np.mean(casc_times):.1f} ms/bar (n={casc_n})")

# ---- projection to full universe ----
n_uni = len(U)
frac_with_surv = (sc>0).mean()
mean_surv = sc.mean()
casc_ms = np.mean(casc_times)
rev_s = np.mean(rev_times)
tickers_active = n_uni * frac_with_surv
total_cascades = tickers_active * mean_surv * 2   # t and t-1
casc_hours = total_cascades * casc_ms / 1000 / 3600
rev_hours = tickers_active * rev_s / 3600
print(f"\n--- projection (single-process, full universe ~{n_uni}) ---")
print(f"tickers with >=1 survivor (proj): {tickers_active:.0f}")
print(f"total cascade calls (x2 for t-1): {total_cascades:,.0f}")
print(f"cascade time: {casc_hours:.1f} h | reversal passes: {rev_hours:.1f} h | total ~{casc_hours+rev_hours:.1f} h")
print(f"with 14 workers: ~{(casc_hours+rev_hours)/14:.1f} h")
