"""Enrich Extension Peek backtest firings with liquidity fields and apply a
'tradable' filter (applied at each firing's entry bar -- no lookahead).

Reads the results JSON, looks up each firing's entry-bar ADR20, 20-day avg
dollar volume, and price from the OHLCV cache, writes an enriched full CSV
plus a tradable-subset CSV, and prints outcome stats on the subset.
"""
import os, sys, json
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner"))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")

from scripts.ext_peek_backtest import _adr20  # same masked 20-bar ADR as the backtest

# tradable thresholds (Dan)
ADR_MIN = 3.5          # ADR% (average daily range, percent)
DVOL_MIN = 40_000_000  # 20-day avg dollar volume
PRICE_MIN = 1.0

RESULTS = os.path.join(CACHE, "ext_peek_backtest_20260603_094408.json")

def main():
    d = json.load(open(RESULTS))
    df = pd.DataFrame(d["trades"])
    print(f"firings in: {len(df)}")

    import pickle
    print("cache:", CACHE)
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        U = pickle.load(f)
    print("universe tickers:", len(U))
    assert len(U) > 11200, "ticker count too low -- STOP"

    # sort by ticker so groupby(sort=True) order matches the row order we fill
    df = df.sort_values("ticker").reset_index(drop=True)
    adr_col, dvol_col = [], []
    for tk, sub in df.groupby("ticker", sort=True):
        o = U.get(tk)
        if o is None:
            adr_col += [np.nan]*len(sub); dvol_col += [np.nan]*len(sub); continue
        dates = o["date"].astype(str).str[:10].values
        h = o["high"].values.astype(np.float64); l = o["low"].values.astype(np.float64)
        adr = _adr20(h, l)
        if "dvol_20d" in o.columns:
            dvol = o["dvol_20d"].values.astype(np.float64)
        else:
            cv = o["close"].values.astype(np.float64) * o["volume"].values.astype(np.float64)
            dvol = pd.Series(cv).rolling(20, min_periods=1).mean().values
        didx = {dd: i for i, dd in enumerate(dates)}
        for dt in sub["date"].values:
            i = didx.get(dt)
            adr_col.append(round(float(adr[i]), 3) if i is not None and not np.isnan(adr[i]) else np.nan)
            dvol_col.append(round(float(dvol[i]), 0) if i is not None and not np.isnan(dvol[i]) else np.nan)
    df["adr20"] = adr_col
    df["dvol_20d"] = dvol_col

    cols = ["ticker","date","slot","entry_close","sig_low","risk","ext_at_entry",
            "adr20","dvol_20d","mfe_abs","mfe_R","mfe_adr","mfe_pct",
            "stopped","bars_to_breach","window_trunc"]
    df = df[cols]
    full_out = os.path.join(CACHE, "ext_peek_backtest_20260603_094408.csv")
    df.to_csv(full_out, index=False)
    print(f"rewrote enriched full CSV ({len(df)} rows) with adr20 + dvol_20d")

    trad = df[(df.adr20 > ADR_MIN) & (df.dvol_20d > DVOL_MIN) & (df.entry_close > PRICE_MIN)].copy()
    trad_out = os.path.join(CACHE, "ext_peek_backtest_20260603_094408_tradable.csv")
    trad.to_csv(trad_out, index=False)
    print(f"\n=== TRADABLE (ADR%>{ADR_MIN}, dvol>${DVOL_MIN/1e6:.0f}M, price>${PRICE_MIN:.0f}) ===")
    print(f"firings: {len(trad)} ({100*len(trad)/len(df):.1f}% of all) | distinct tickers: {trad.ticker.nunique()}")
    if len(trad):
        R = trad.mfe_R.values; st = trad.stopped.values.astype(bool)
        btb = trad.bars_to_breach.fillna(10**9).values
        print(f"stopped(loss): {st.sum()} ({100*st.mean():.1f}%) | survivors: {(~st).sum()} ({100*(~st).mean():.1f}%)")
        print("stop timing:", "  ".join(f"<= {dd}d {100*(btb<=dd).mean():.0f}%" for dd in (1,3,5,10,20)))
        print(f"MFE R: median={np.median(R):.2f} p75={np.percentile(R,75):.2f} p90={np.percentile(R,90):.2f} mean={R.mean():.2f} max={R.max():.0f}")
        for thr in (1,2,3,5):
            print(f"  >= {thr}R: {(R>=thr).sum()} ({100*(R>=thr).mean():.1f}%)")
    print(f"\nwrote {trad_out}")

if __name__ == "__main__":
    main()
