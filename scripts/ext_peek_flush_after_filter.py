"""Extension Peek -- invalidate the breaking trendline if a downside-1/2 flush
occurred AFTER its origin (a reset spanned the line = stale).

For each firing's entry/peek line: scan from the line's origin (i0) to the break
bar (t); if the extension reached downside_1 (or deeper downside_2) anywhere in
that span, the line is invalid -> drop the setup. Reports removed count, the
valid-set stats, and valid-vs-invalid outcome comparison. Read-only.
"""
import os, sys, json, pickle
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner")); sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")
from scripts.ext_peek_backtest import _adr20, _sma, _ext50
WINDOW = 60
LV = ("upside_1","upside_2","downside_1","downside_2","chop_upper")

def _clean_desc(ext, b, ls):
    from scripts.ext50_trendlines import cascade_at, _has_line_break
    snap = cascade_at(ext, b, ls)
    out = [c for c in (snap.get("all_candidates") or [])
           if c["anchor_type"]=="peak_anchored"
           and not _has_line_break(ext, c["i0"],c["v0"],c["i1"],c["v1"], b)]
    out.sort(key=lambda c: abs(c["signed_dist"]))
    return out

def _flush_after(ext, ds1, ds2, i0, t):
    """Did ext reach downside_1 / downside_2 anywhere in (i0, t]?"""
    a, b = i0+1, t+1
    if a >= b: return (False, False)
    se = ext[a:b]; s1 = ds1[a:b]; s2 = ds2[a:b]
    m1 = (~np.isnan(se)) & (~np.isnan(s1))
    m2 = (~np.isnan(se)) & (~np.isnan(s2))
    f1 = bool(np.any(se[m1] <= s1[m1])) if m1.any() else False
    f2 = bool(np.any(se[m2] <= s2[m2])) if m2.any() else False
    return (f1, f2)

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
            top3=lines[:3]; el=None
            for u in top3:
                if (u["v1"]+u["slope"]*(t-u["i1"]) - ext[t])<0 and u["signed_dist"]>=0:
                    el=u; break
            if el is None and top3: el=top3[0]
            fa1=fa2=False; ei0=None
            if el is not None:
                ei0=int(el["i0"]); fa1,fa2=_flush_after(ext,ds1,ds2,ei0,t)
            rec={"ticker":tk,"trigger_date":dt,"entry_i0":ei0,
                 "flush_after1":fa1,"flush_after2":fa2,"valid":(not fa1),
                 "filled":False,"mfe_R":np.nan,"r_at_day5":np.nan,"touched1R_by5":np.nan,"stopped":np.nan}
            trig_hi,trig_lo=h[t],l[t]; j=None; canc=False
            for jj in range(t+1,n):
                if l[jj]<trig_lo: canc=True; break
                if h[jj]>trig_hi: j=jj; break
            if (not canc) and j is not None:
                entry=op[j] if op[j]>trig_hi else trig_hi; risk=entry-trig_lo
                if risk>0:
                    rec["filled"]=True; end=min(j+WINDOW,n-1); best=h[j]; st=False
                    for k in range(j+1,end+1):
                        if l[k]<trig_lo: st=True; break
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
    inv=out[out.flush_after1]; val=out[~out.flush_after1]
    print(f"\n=== FLUSH-AFTER-ORIGIN INVALIDATION (firings={n}, errors={len(errs)}) ===")
    print(f"INVALID (downside-1/2 flush after the breaking line's origin): {len(inv)} ({100*len(inv)/n:.1f}%)")
    print(f"  ... of which reached downside_2: {out.flush_after2.sum()} ({100*out.flush_after2.mean():.1f}%)")
    print(f"VALID (kept): {len(val)} ({100*len(val)/n:.1f}%)  tickers={val.ticker.nunique()}")
    def stats(c,label):
        f=c[c.filled]
        if not len(f): print(f"  [{label}] n={len(c)} no fills"); return
        d5=f.dropna(subset=["r_at_day5"])
        print(f"  [{label}] firings={len(c)} filled={len(f)} | >=1R MFE={100*(f.mfe_R>=1).mean():.1f}% >=3R={100*(f.mfe_R>=3).mean():.1f}% medMFE={f.mfe_R.median():.2f} | day5>=1R={100*(d5.r_at_day5>=1).mean():.1f}% touched1R/5d={100*d5.touched1R_by5.mean():.1f}% stopped60={100*f.stopped.mean():.1f}%")
    print("\n-- compare --")
    stats(val,"VALID (kept)")
    stats(inv,"INVALID (removed)")
    stats(out,"ALL (baseline)")
    val.to_csv(os.path.join(CACHE,"ext_peek_valid_no_flush_after.csv"),index=False)
    print(f"\nwrote {os.path.join(CACHE,'ext_peek_valid_no_flush_after.csv')}")

if __name__=="__main__":
    main()
