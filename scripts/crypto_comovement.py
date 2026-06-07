"""Does each crypto-miner name trade with the AI/neocloud cohort or with Bitcoin?
Route by co-movement, not press releases (the chart is the tell). Correlate each
name's daily returns against (1) a pure-neocloud basket (CRWV+NBIS) and (2) a
Bitcoin proxy, over recent windows; also show whether BTC-correlation is decaying
(decoupling = pivoting for real). Read-only over the OHLCV cache. ASCII output."""
import pickle
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(r"C:\Users\Dan\Documents\ScanPerfect\swing-screener")
CACHE = REPO / "local_runner" / "cache" / "universe_ohlcv_daily.pkl"

with open(CACHE, "rb") as f:
    cache = pickle.load(f)
print(f"cache: {CACHE}")
print(f"tickers in cache: {len(cache)}")

def ret(tk):
    """Daily pct-change of close, date-indexed."""
    if tk not in cache:
        return None
    d = cache[tk].copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").set_index("date")
    s = d[~d.index.duplicated(keep="last")]["close"].astype(float)
    return s.pct_change()

def basket(tks):
    rs = [ret(t) for t in tks]
    rs = [r for r in rs if r is not None]
    if not rs:
        return None
    return pd.concat(rs, axis=1).mean(axis=1, skipna=True)

# ---- reference cohorts ----
AI_MEMBERS = [t for t in ["CRWV", "NBIS"] if t in cache]
ai = basket(AI_MEMBERS)
btc_candidates = ["IBIT", "FBTC", "BITO", "GBTC", "BTC-USD", "MSTR"]
btc_tk = next((t for t in btc_candidates if t in cache), None)
btc = ret(btc_tk) if btc_tk else None
miner = basket(["CLSK", "MARA", "RIOT"])   # pure-crypto-miner anchor (cross-check)

print(f"AI / neocloud anchor: {AI_MEMBERS}")
print(f"Bitcoin proxy:        {btc_tk}")
print(f"pure-miner anchor:    CLSK+MARA+RIOT")
if ai is None or btc is None:
    raise SystemExit("missing a reference cohort; cannot run")

TEST = ["MARA", "RIOT", "HUT", "CLSK", "CIFR", "CORZ", "WULF", "IREN", "APLD",
        "SLNH", "BTDR", "DGXX", "HIVE", "KEEL", "GLXY", "BTBT"]
missing = [t for t in TEST if t not in cache]
present = [t for t in TEST if t in cache]
if missing:
    print(f"NOT in cache (skipped): {missing}")

def corr(a, b, n):
    m = pd.concat([a, b], axis=1).dropna()
    if len(m) < 12:
        return np.nan
    m = m.iloc[-n:]
    if len(m) < max(12, int(n * 0.6)):
        return np.nan
    return m.iloc[:, 0].corr(m.iloc[:, 1])

def corr_prior(a, b, n):
    """corr over the window BEFORE the most-recent n bars (to see decoupling)."""
    m = pd.concat([a, b], axis=1).dropna()
    if len(m) < 2 * n:
        return np.nan
    m = m.iloc[-2 * n:-n]
    return m.iloc[:, 0].corr(m.iloc[:, 1])

def verdict(cai, cbtc):
    if np.isnan(cai) or np.isnan(cbtc):
        return "?"
    d = cai - cbtc
    if d > 0.07:
        return "AI"
    if d < -0.07:
        return "BTC"
    return "mixed"

for label, n in [("last ~3 months (63 bars)", 63), ("last ~6 months (126 bars)", 126)]:
    print("\n" + "=" * 78)
    print(f"CO-MOVEMENT  -  {label}")
    print("=" * 78)
    print(f"{'name':6s} {'corrAI':>7s} {'corrBTC':>8s} {'corrMiner':>10s} "
          f"{'AI-BTC':>7s}  verdict")
    rows = []
    for t in present:
        r = ret(t)
        cai = corr(r, ai, n)
        cbtc = corr(r, btc, n)
        cmin = corr(r, miner, n)
        rows.append((t, cai, cbtc, cmin, (cai - cbtc) if not (np.isnan(cai) or np.isnan(cbtc)) else np.nan))
    rows.sort(key=lambda x: (-(x[4]) if not np.isnan(x[4]) else 1e9))
    for t, cai, cbtc, cmin, tilt in rows:
        print(f"{t:6s} {cai:7.2f} {cbtc:8.2f} {cmin:10.2f} {tilt:7.2f}  {verdict(cai, cbtc)}")

# decoupling: did BTC-correlation fall from the prior 63 bars to the recent 63?
print("\n" + "=" * 78)
print("DECOUPLING FROM BITCOIN  (BTC-corr: prior 63 bars -> recent 63 bars)")
print("negative drop = pulling away from crypto (pivoting for real)")
print("=" * 78)
print(f"{'name':6s} {'prior':>7s} {'recent':>7s} {'drop':>7s}")
drows = []
for t in present:
    r = ret(t)
    pri = corr_prior(r, btc, 63)
    rec = corr(r, btc, 63)
    drows.append((t, pri, rec, (rec - pri) if not (np.isnan(pri) or np.isnan(rec)) else np.nan))
drows.sort(key=lambda x: (x[3] if not np.isnan(x[3]) else 1e9))
for t, pri, rec, drop in drows:
    print(f"{t:6s} {pri:7.2f} {rec:7.2f} {drop:7.2f}")

# sanity check: the anchors themselves
print("\n" + "=" * 78)
print("SANITY  -  the reference names (should clearly separate)")
print("=" * 78)
print(f"{'name':6s} {'corrAI':>7s} {'corrBTC':>8s}  (126 bars)")
for t in AI_MEMBERS + [btc_tk, "CLSK", "MARA"]:
    r = ret(t)
    if r is None:
        continue
    print(f"{t:6s} {corr(r, ai, 126):7.2f} {corr(r, btc, 126):8.2f}")
