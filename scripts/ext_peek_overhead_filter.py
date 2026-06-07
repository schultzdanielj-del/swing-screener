"""Extension Peek -- 'no overhead descending line' filter.

For each firing, project EVERY valid (clean, unbroken) descending trendline to
the signal bar and require price (ext) to be above ALL of them. Firings with any
valid downsloping line still at/above price are flagged 'under a line'.

Operates on the tradable, no-biotech/XLE working set. Re-simulates the
confirmation entry + day-5 outcome per firing so we can compare clean-break vs
under-a-line. Read-only; writes a per-firing CSV.
"""
import os, sys, json, pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner"))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")

from scripts.ext_peek_backtest import _adr20, _sma, _ext50
WINDOW = 60
LV_KEYS = ("upside_1","upside_2","downside_1","downside_2","chop_upper")

def _clean_descending_all(ext, asof_bar, levels_scalar):
    from scripts.ext50_trendlines import cascade_at, _has_line_break
    snap = cascade_at(ext, asof_bar, levels_scalar)
    out = []
    for c in (snap.get("all_candidates") or []):
        if c["anchor_type"] == "peak_anchored":
            if _has_line_break(ext, c["i0"], c["v0"], c["i1"], c["v1"], asof_bar):
                continue
            out.append(c)
    return out

def _process(args):
    tk, df, dates = args
    try:
        if df is None or len(df) < 260:
            return tk, [], "short"
        d = df["date"].astype(str).str[:10].values
        c = df["close"].values.astype(np.float64); h = df["high"].values.astype(np.float64)
        l = df["low"].values.astype(np.float64); op = df["open"].values.astype(np.float64)
        ext = _ext50(c, _sma(c,50), _adr20(h,l))
        from scripts.reversal_profile import compute_all_reversal_profile_series
        rp = compute_all_reversal_profile_series(ext)
        lv = {k: rp.get(k) for k in LV_KEYS}
        didx = {dd:i for i,dd in enumerate(d)}; n = len(c)
        recs = []
        for dt in dates:
            t = didx.get(dt)
            if t is None or t < 1:
                continue
            b = t - 1
            ls = {k: (float(lv[k][b]) if lv[k] is not None and b < len(lv[k]) and not np.isnan(lv[k][b]) else float("nan")) for k in lv}
            lines = _clean_descending_all(ext, b, ls)
            ext_t = ext[t]
            n_above = sum(1 for u in lines if (u["v1"] + u["slope"]*(t - u["i1"])) >= ext_t - 1e-9)
            rec = {"ticker": tk, "trigger_date": dt, "clean": n_above == 0,
                   "n_desc": len(lines), "n_above": n_above, "filled": False,
                   "mfe_R": np.nan, "stopped": np.nan, "bars_to_breach": np.nan,
                   "r_at_day5": np.nan, "touched1R_by5": np.nan, "stopped_by5": np.nan}
            trig_hi, trig_lo = h[t], l[t]
            j = None; cancelled = False
            for jj in range(t+1, n):
                if l[jj] < trig_lo: cancelled = True; break
                if h[jj] > trig_hi: j = jj; break
            if (not cancelled) and j is not None:
                entry = op[j] if op[j] > trig_hi else trig_hi
                risk = entry - trig_lo
                if risk > 0:
                    rec["filled"] = True
                    end = min(j+WINDOW, n-1); best = h[j]; stopped = False; br = None
                    for k in range(j+1, end+1):
                        if l[k] < trig_lo: stopped = True; br = k; break
                        if h[k] > best: best = h[k]
                    mfe = max(0.0, best - entry)
                    rec["mfe_R"] = mfe/risk; rec["stopped"] = bool(stopped)
                    rec["bars_to_breach"] = (br-j) if br is not None else np.nan
                    if j+5 <= n-1:
                        f_lo = l[j+1:j+6]; f_hi = h[j+1:j+6]
                        rec["r_at_day5"] = (c[j+5]-entry)/risk
                        rec["stopped_by5"] = bool((f_lo < trig_lo).any())
                        rec["touched1R_by5"] = bool(f_hi.max() >= entry+risk)
            recs.append(rec)
        return tk, recs, None
    except Exception as e:
        return tk, [], repr(e)

def main():
    fund = json.load(open(os.path.join(CACHE, "fundamentals_cache.json")))["tickers"]
    def excl(t):
        e = fund.get(t)
        return bool(e) and (e.get("industry")=="Biotechnology" or
                            (e.get("sector")=="Energy" and str(e.get("industry") or "").startswith("Oil & Gas")))
    trad = pd.read_csv(os.path.join(CACHE, "ext_peek_backtest_20260603_094408_tradable.csv"))
    trad = trad[~trad.ticker.map(excl)]
    print(f"working firings (tradable, no bio/XLE): {len(trad)}  tickers={trad.ticker.nunique()}")
    print("cache:", CACHE)
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        U = pickle.load(f)
    print("universe tickers:", len(U)); assert len(U) > 11200
    items = [(tk, U.get(tk), list(sub.date.values)) for tk, sub in trad.groupby("ticker", sort=True)]

    rows = []; errs = {}
    with ProcessPoolExecutor(max_workers=12) as exe:
        futs = [exe.submit(_process, it) for it in items]
        for fut in as_completed(futs):
            tk, recs, err = fut.result()
            if err: errs[tk] = err
            rows.extend(recs)
    out = pd.DataFrame(rows)
    n = len(out)
    clean = out[out.clean]; under = out[~out.clean]
    print(f"\n=== OVERHEAD-LINE FILTER (firings={n}, errors={len(errs)}) ===")
    print(f"clean break (above ALL descending lines): {len(clean)} ({100*len(clean)/n:.1f}%)")
    print(f"under >=1 valid descending line:          {len(under)} ({100*len(under)/n:.1f}%)")

    def stats(c, label):
        f = c[c.filled]
        if not len(f): print(f"  [{label}] no filled"); return
        d5 = f.dropna(subset=["r_at_day5"])
        s5 = f[(f.stopped==True) & (f.bars_to_breach<=5)]
        print(f"  [{label}] firings={len(c)} filled={len(f)} "
              f"| >=1R MFE(60)={100*(f.mfe_R>=1).mean():.1f}% >=3R={100*(f.mfe_R>=3).mean():.1f}% medMFE={f.mfe_R.median():.2f} "
              f"| day5>=1R={100*(d5.r_at_day5>=1).mean():.1f}% touched1R/5d={100*d5.touched1R_by5.mean():.1f}% "
              f"| fast-stop never-1R={100*(s5.mfe_R<1).mean():.1f}%")
    print("\n-- compare --")
    stats(clean, "CLEAN break")
    stats(under, "UNDER a line")
    stats(out,   "ALL (baseline)")

    out.to_csv(os.path.join(CACHE, "ext_peek_overhead_filtered.csv"), index=False)
    print(f"\nwrote {os.path.join(CACHE,'ext_peek_overhead_filtered.csv')}")

if __name__ == "__main__":
    main()
