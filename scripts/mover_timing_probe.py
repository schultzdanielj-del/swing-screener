"""Ad-hoc: date each mover's 10/20 SMA bullish cross (both MAs rising) and
compare timing across theme buckets. Read-only over the OHLCV cache."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

CACHE = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener\local_runner\cache\universe_ohlcv_daily.pkl")

BUCKETS = {
    "A  AI-semis/hardware":   ["MRVL","QCOM","INTC","MU","GFS","NVTS","MXL","SMTC","AOSL","POET","CEVA","ACMR","SNDK","STX","VSH","VICR","NTAP","EXTR","DELL","HPE","SMCI"],
    "B  Software/cyber":      ["FROG","DDOG","TENB","TWLO","TEAM","NAVN","RBRK","FTNT","GTLB","CRWD","BB"],
    "C  Space/drones/def":    ["RKLB","RDW","UMAC","LUNR","SATL"],
    "D  Crypto->AI DC":       ["BTDR","DGXX","DXYZ"],
    "E  Autonomous/sensing":  ["AEVA","AUR"],
    "F  Power/energy":        ["SEDG","TE","FCEL","SGML","GTX"],
    "G  Healthcare":          ["OSCR","HUM","GH","HNGE"],
    "H  Outliers":            ["CAR","SG"],
}

LOOKBACK_CROSS = 220  # trading days back to search for the initiating cross
SLOPE_K = 3           # bars used for the "angling up" slope check

with open(CACHE, "rb") as f:
    cache = pickle.load(f)

print("CACHE:", CACHE)
print("tickers in cache:", len(cache))

all_tk = [t for b in BUCKETS.values() for t in b]
missing = [t for t in all_tk if t not in cache]
print(f"requested: {len(all_tk)} | present: {len(all_tk)-len(missing)} | missing: {missing}")

# global last bar date (use the most common last date across requested names)
last_dates = []
for t in all_tk:
    if t in cache:
        last_dates.append(pd.to_datetime(cache[t]["date"]).iloc[-1])
if last_dates:
    print("latest bar (mode):", pd.Series(last_dates).mode().iloc[0].date())
print()


def cross_info(df):
    dates = pd.to_datetime(df["date"]).reset_index(drop=True)
    close = pd.Series(df["close"].astype(float).values)
    n = len(close)
    if n < 40:
        return None
    sma10 = close.rolling(10).mean().values
    sma20 = close.rolling(20).mean().values
    diff = sma10 - sma20
    start = max(SLOPE_K + 1, n - LOOKBACK_CROSS)
    best = None
    for i in range(start, n):
        if np.isnan(diff[i]) or np.isnan(diff[i-1]):
            continue
        if diff[i] > 0 and diff[i-1] <= 0:                 # bullish cross
            if sma10[i] > sma10[i-SLOPE_K] and sma20[i] > sma20[i-SLOPE_K]:  # both rising
                best = i                                    # keep most-recent qualifying
    still_above = bool(diff[-1] > 0)
    if best is None:
        return {"date": None, "days_ago": None, "still_above": still_above}
    return {"date": dates.iloc[best], "days_ago": int(n - 1 - best), "still_above": still_above}


rows = {}
for bucket, tickers in BUCKETS.items():
    rows[bucket] = []
    for t in tickers:
        if t not in cache:
            rows[bucket].append((t, None, None, None))
            continue
        info = cross_info(cache[t])
        if info is None or info["date"] is None:
            rows[bucket].append((t, None, info["still_above"] if info else None, None))
        else:
            rows[bucket].append((t, info["date"], info["still_above"], info["days_ago"]))

print("=" * 78)
print("PER-TICKER  (10/20 bullish cross, both MAs rising; most recent within 90 td)")
print("=" * 78)
for bucket, r in rows.items():
    print(f"\n{bucket}")
    r_sorted = sorted(r, key=lambda x: (x[1] is None, x[1] if x[1] is not None else pd.Timestamp.max))
    for t, dt, above, ago in r_sorted:
        if dt is None:
            state = "10>20 now (trending before window)" if above else "10<20 now (not in uptrend)"
            print(f"   {t:6s}  no cross in window  [{state}]")
        else:
            flag = "" if above else "  (10<20 now)"
            print(f"   {t:6s}  {dt.date()}   {ago:>3d} td ago{flag}")

print("\n" + "=" * 78)
print("BUCKET SUMMARY  (sorted earliest median cross first)")
print("=" * 78)
summ = []
for bucket, r in rows.items():
    dd = [x[1] for x in r if x[1] is not None]
    if not dd:
        summ.append((bucket, None, None, None, 0, len(r)))
        continue
    ords = sorted(d.toordinal() for d in dd)
    med = pd.Timestamp.fromordinal(int(np.median(ords)))
    summ.append((bucket, med, min(dd), max(dd), len(dd), len(r)))

summ.sort(key=lambda s: (s[1] is None, s[1] if s[1] is not None else pd.Timestamp.max))
for bucket, med, lo, hi, ncross, ntot in summ:
    if med is None:
        print(f"{bucket:24s}  no crosses")
    else:
        print(f"{bucket:24s}  median {med.date()}   range {lo.date()} -> {hi.date()}   ({ncross}/{ntot} crossed)")
