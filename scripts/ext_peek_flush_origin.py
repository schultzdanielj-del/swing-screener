"""Extension Peek -- 'origin born from a downside flush' classification.

For each firing's descending lines, find the origin peak (i0), locate the trough
the rally into it began from (last prominence-0.5 trough before i0), and check
whether that trough reached the downside_1 (or deeper downside_2) reversal level.
A line whose origin peak rallied up out of a downside-1/2 flush = "born from flush".

Reports: firing-level on the entry/peek line, line-level across all descending
lines, and the confirmation-entry day-5 outcome split by it. Read-only.
"""
import os, sys, json, pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner")); sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")
from scripts.ext_peek_backtest import _adr20, _sma, _ext50
WINDOW = 60; PROM = 0.5
LV = ("upside_1","upside_2","downside_1","downside_2","chop_upper")

def _clean_desc(ext, b, ls):
    from scripts.ext50_trendlines import cascade_at, _has_line_break
    snap = cascade_at(ext, b, ls)
    out = [c for c in (snap.get("all_candidates") or [])
           if c["anchor_type"]=="peak_anchored"
           and not _has_line_break(ext, c["i0"],c["v0"],c["i1"],c["v1"], b)]
    out.sort(key=lambda c: abs(c["signed_dist"]))
    return out

def _flush_before(ext, ds1, ds2, i0):
    """Did the rally into peak i0 start from a downside_1/2 flush?"""
    from scipy.signal import find_peaks
    if i0 < 3: return (False, False, None)
    e = ext[:i0].copy()
    neg = -np.where(np.isnan(e), np.inf, e)   # peaks of neg = troughs of ext
    tr,_ = find_peaks(neg, prominence=PROM)
    b = int(tr[-1]) if len(tr) else (int(np.nanargmin(e)) if np.isfinite(np.nanmin(e)) else None)
    if b is None: return (False, False, None)
    ev = ext[b]
    d1 = ds1[b] if b < len(ds1) else np.nan
    d2 = ds2[b] if b < len(ds2) else np.nan
    r1 = (not np.isnan(d1)) and (ev <= d1)
    r2 = (not np.isnan(d2)) and (ev <= d2)
    return (bool(r1), bool(r2), b)

def _process(args):
    tk, df, dates = args
    try:
        if df is None or len(df) < 260: return tk, [], "short"
        d = df["date"].astype(str).str[:10].values
        c=df["close"].values.astype(float); h=df["high"].values.astype(float)
        l=df["low"].values.astype(float); op=df["open"].values.astype(float)
        ext=_ext50(c,_sma(c,50),_adr20(h,l))
        from scripts.reversal_profile import compute_all_reversal_profile_series
        rp=compute_all_reversal_profile_series(ext)
        ds1=rp.get("downside_1"); ds2=rp.get("downside_2")
        if ds1 is None: ds1=np.full(len(c),np.nan)
        if ds2 is None: ds2=np.full(len(c),np.nan)
        didx={dd:i for i,dd in enumerate(d)}; n=len(c)
        recs=[]
        for dt in dates:
            t=didx.get(dt)
            if t is None or t<1: continue
            b=t-1
            ls={k:(float(rp[k][b]) if rp.get(k) is not None and b<len(rp[k]) and not np.isnan(rp[k][b]) else float("nan")) for k in LV}
            lines=_clean_desc(ext,b,ls)
            # entry/peek line = first (tightest) of top-3 that crossed into t
            top3=lines[:3]; entry_line=None
            for u in top3:
                proj_t=u["v1"]+u["slope"]*(t-u["i1"])
                if (proj_t-ext[t])<0 and u["signed_dist"]>=0:
                    entry_line=u; break
            if entry_line is None and top3:  # fallback: tightest
                entry_line=top3[0]
            ef1=ef2=False; ei0=None
            if entry_line is not None:
                ei0=int(entry_line["i0"]); ef1,ef2,_=_flush_before(ext,ds1,ds2,ei0)
            # line-level across all descending
            nflush1=nflush2=0
            for u in lines:
                f1,f2,_=_flush_before(ext,ds1,ds2,int(u["i0"]))
                nflush1+=f1; nflush2+=f2
            rec={"ticker":tk,"trigger_date":dt,"n_desc":len(lines),
                 "entry_i0":ei0,"entry_flush1":ef1,"entry_flush2":ef2,
                 "n_desc_flush1":nflush1,"n_desc_flush2":nflush2,
                 "filled":False,"mfe_R":np.nan,"r_at_day5":np.nan,"touched1R_by5":np.nan,"stopped":np.nan}
            # confirm entry + day5
            trig_hi,trig_lo=h[t],l[t]; j=None; canc=False
            for jj in range(t+1,n):
                if l[jj]<trig_lo: canc=True; break
                if h[jj]>trig_hi: j=jj; break
            if (not canc) and j is not None:
                entry=op[j] if op[j]>trig_hi else trig_hi; risk=entry-trig_lo
                if risk>0:
                    rec["filled"]=True; end=min(j+WINDOW,n-1); best=h[j]; st=False; br=None
                    for k in range(j+1,end+1):
                        if l[k]<trig_lo: st=True; br=k; break
                        if h[k]>best: best=h[k]
                    rec["mfe_R"]=max(0.0,best-entry)/risk; rec["stopped"]=bool(st)
                    if j+5<=n-1:
                        rec["r_at_day5"]=(c[j+5]-entry)/risk
                        rec["touched1R_by5"]=bool(h[j+1:j+6].max()>=entry+risk)
            recs.append(rec)
        return tk,recs,None
    except Exception as e:
        return tk,[],repr(e)

def main():
    fund=json.load(open(os.path.join(CACHE,"fundamentals_cache.json")))["tickers"]
    def excl(t):
        e=fund.get(t); return bool(e) and (e.get("industry")=="Biotechnology" or (e.get("sector")=="Energy" and str(e.get("industry") or "").startswith("Oil & Gas")))
    trad=pd.read_csv(os.path.join(CACHE,"ext_peek_backtest_20260603_094408_tradable.csv"))
    trad=trad[~trad.ticker.map(excl)]
    print(f"working firings: {len(trad)} tickers={trad.ticker.nunique()}")
    with open(os.path.join(CACHE,"universe_ohlcv_daily.pkl"),"rb") as f: U=pickle.load(f)
    print("universe tickers:",len(U)); assert len(U)>11200
    items=[(tk,U.get(tk),list(sub.date.values)) for tk,sub in trad.groupby("ticker",sort=True)]
    rows=[]; errs={}
    with ProcessPoolExecutor(max_workers=12) as exe:
        futs=[exe.submit(_process,it) for it in items]
        for fut in as_completed(futs):
            tk,recs,err=fut.result()
            if err: errs[tk]=err
            rows.extend(recs)
    out=pd.DataFrame(rows); n=len(out)
    tot_lines=out.n_desc.sum()
    print(f"\n=== ORIGIN BORN FROM DOWNSIDE FLUSH (firings={n}, errors={len(errs)}) ===")
    print(f"ENTRY line born from flush: downside_1+={out.entry_flush1.sum()} ({100*out.entry_flush1.mean():.1f}%)  "
          f"downside_2={out.entry_flush2.sum()} ({100*out.entry_flush2.mean():.1f}%)")
    print(f"ALL descending lines: {tot_lines} total; born from downside_1+ flush={out.n_desc_flush1.sum()} ({100*out.n_desc_flush1.sum()/max(tot_lines,1):.1f}%)  downside_2={out.n_desc_flush2.sum()} ({100*out.n_desc_flush2.sum()/max(tot_lines,1):.1f}%)")
    def stats(c,label):
        f=c[c.filled];
        if not len(f): print(f"  [{label}] n={len(c)} no fills"); return
        d5=f.dropna(subset=["r_at_day5"])
        print(f"  [{label}] firings={len(c)} filled={len(f)} | >=1R MFE={100*(f.mfe_R>=1).mean():.1f}% >=3R={100*(f.mfe_R>=3).mean():.1f}% medMFE={f.mfe_R.median():.2f} | day5>=1R={100*(d5.r_at_day5>=1).mean():.1f}% touched1R/5d={100*d5.touched1R_by5.mean():.1f}%")
    print("\n-- outcome split by ENTRY-line origin --")
    stats(out[out.entry_flush1],"entry born from downside_1+ flush")
    stats(out[~out.entry_flush1],"entry NOT born from flush")
    stats(out,"ALL")
    out.to_csv(os.path.join(CACHE,"ext_peek_flush_origin.csv"),index=False)
    print(f"\nwrote {os.path.join(CACHE,'ext_peek_flush_origin.csv')}")

if __name__=="__main__":
    main()
