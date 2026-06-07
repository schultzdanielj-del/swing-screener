"""Is the recent rip in the miner names a CRYPTO rip (whole pack runs together) or
an AI rip (a name breaks away toward the neoclouds while the pack lags)? Show short-
window cumulative returns + short-window correlation tilt, so 'X is ripping' can be
read as 'ripping with crypto' vs 'ripping with AI'. Read-only. ASCII output."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print(f"cache tickers: {len(cache)}")

def closes(tk):
    if tk not in cache:
        return None
    d = cache[tk].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    return d[~d.index.duplicated(keep="last")]["close"].astype(float)

def ret(tk):
    c = closes(tk)
    return None if c is None else c.pct_change()

def basket_ret(tks):
    rs = [ret(t) for t in tks if ret(t) is not None]
    return pd.concat(rs, axis=1).mean(axis=1, skipna=True) if rs else None

def cum_ret(tk, n):
    c = closes(tk)
    if c is None or len(c) < n + 1:
        return np.nan
    return (c.iloc[-1] / c.iloc[-1 - n] - 1.0) * 100.0

AI = basket_ret(["CRWV", "NBIS"])
BTC = ret("IBIT")
# pure-miner pack EXCLUDING RIOT so we can see if RIOT moves with it or away
MINERS_EXRIOT = basket_ret(["CLSK", "MARA", "HIVE"])
last_date = closes("IBIT").index[-1]
print(f"last bar: {last_date.date()}")

def corr_tilt(tk, n):
    r = ret(tk)
    if r is None:
        return np.nan, np.nan
    def c(a, b):
        m = pd.concat([a, b], axis=1).dropna().iloc[-n:]
        return m.iloc[:, 0].corr(m.iloc[:, 1]) if len(m) >= 8 else np.nan
    return c(r, AI), c(r, BTC)

print("\nANCHORS  -  recent cumulative return (%):")
print(f"{'':16s} {'10d':>7s} {'21d':>7s} {'42d':>7s}")
for nm, tks in [("neoclouds CRWV/NBIS", ["CRWV", "NBIS"]),
                ("miner pack (ex-RIOT)", ["CLSK", "MARA", "HIVE"]),
                ("Bitcoin (IBIT)", ["IBIT"])]:
    r10 = np.nanmean([cum_ret(t, 10) for t in tks])
    r21 = np.nanmean([cum_ret(t, 21) for t in tks])
    r42 = np.nanmean([cum_ret(t, 42) for t in tks])
    print(f"{nm:16s} {r10:7.1f} {r21:7.1f} {r42:7.1f}")

NAMES = ["RIOT", "CORZ", "IREN", "APLD", "HUT", "CIFR", "WULF",
         "MARA", "CLSK", "HIVE", "BTDR", "BTBT", "GLXY", "KEEL", "SLNH", "DGXX"]
print("\nNAMES  -  recent return (%)  +  21d co-movement tilt (corrAI - corrBTC):")
print(f"{'name':6s} {'10d':>7s} {'21d':>7s} {'42d':>7s} {'cAI21':>6s} {'cBTC21':>7s} {'tilt':>6s}")
for t in NAMES:
    if t not in cache:
        print(f"{t:6s}  (not in cache)")
        continue
    cai, cbtc = corr_tilt(t, 21)
    tilt = (cai - cbtc) if not (np.isnan(cai) or np.isnan(cbtc)) else np.nan
    print(f"{t:6s} {cum_ret(t,10):7.1f} {cum_ret(t,21):7.1f} {cum_ret(t,42):7.1f} "
          f"{cai:6.2f} {cbtc:7.2f} {tilt:6.2f}")
