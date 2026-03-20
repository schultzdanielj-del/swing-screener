"""Optimized 2-stage trim search for profit_grinder.py.

Outer loop = expressions (12,878), inner = final exits (50).
Extracts each column ONCE. 50x fewer extractions than per-final-exit.

Same function signature as the inline grind_2stage in profit_grinder.py
so main() can call either without changes.
"""
import time, numpy as np

TRIM_PCTS = [0.33, 0.50, 0.67]
LOSS_ADR = 1.0
N_TH = 100

def _ecp(fwd_expr_list, valid_indices, expr_col, exit_horizon):
    n=len(valid_indices); c=np.full((n,exit_horizon),np.nan,dtype=np.float32)
    for vi,si in enumerate(valid_indices): fe=fwd_expr_list[si]; c[vi,:fe.shape[0]]=fe[:,expr_col]
    return c

def _eeb(entry_high_bar_expr_list, valid_indices, expr_col):
    n=len(valid_indices); v=np.full(n,np.nan,dtype=np.float32)
    for vi,si in enumerate(valid_indices):
        eb=entry_high_bar_expr_list[si]
        if eb is not None: v[vi]=eb[expr_col]
    return v

def _get_ram():
    try:
        import psutil; return psutil.virtual_memory().available/(1024**3)
    except ImportError:
        try:
            import ctypes,sys
            if sys.platform=='win32':
                k=ctypes.windll.kernel32; u=ctypes.c_ulonglong
                class M(ctypes.Structure):
                    _fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),('ullTotalPhys',u),('ullAvailPhys',u),('ullTotalPageFile',u),('ullAvailPageFile',u),('ullTotalVirtual',u),('ullAvailVirtual',u),('ullAvailExtendedVirtual',u)]
                s=M(); s.dwLength=ctypes.sizeof(s); k.GlobalMemoryStatusEx(ctypes.byref(s))
                return s.ullAvailPhys/(1024**3)
        except: pass
    return None

def _check(label,mn=1.0):
    import sys
    a=_get_ram()
    if a is not None and a<mn: print(f"\n  RAM ABORT {label}: {a:.1f}GB"); sys.exit(1)

def _pram(label):
    a=_get_ram()
    if a is not None: print(f"  RAM: {a:.1f} GB {label}")


def grind_2stage(stage1_results, fwd_expr_list, fwd_close_list,
                entry_high_bar_expr_list, entry_high_offset_v, valid_indices,
                close_2d, entry_prices_v, adr_values_v, weights_v,
                is_hard_gate_v, move_adrs_v, n_bars_per_signal,
                filtered_names, direction, exit_horizon, top_n_final=50):
    # Import stats from the running module (already loaded as __main__)
    import __main__ as pg
    stats_fn = pg.compute_weighted_stats

    nv=len(valid_indices); ne=len(filtered_names)
    n_final=min(top_n_final, len(stage1_results))
    if n_final==0: print("\n  -- 2-STAGE: No results --"); return []

    print(f"\n  -- 2-STAGE TRIM SEARCH (optimized) --")
    print(f"  {ne} trim exprs x {n_final} final exits x ~{N_TH} thresholds x 2 dirs x {len(TRIM_PCTS)} trim%")
    print(f"  Trim%: {[f'{p:.0%}' for p in TRIM_PCTS]}  (optional)")
    _pram("(before 2-stage)"); t0=time.time()

    bi=np.arange(exit_horizon)[np.newaxis,:]

    # Pre-compute final exit profiles
    print(f"  Pre-computing {n_final} final exit profiles...")
    final_data=[]
    for fi in range(n_final):
        f=stage1_results[fi]; fn=f["expr_name"]; fd=f["direction"]; ft=f["threshold"]
        fei=None
        for j,nm in enumerate(filtered_names):
            if nm==fn: fei=j; break
        if fei is None: continue
        fcol=_ecp(fwd_expr_list,valid_indices,fei,exit_horizon)
        fsr=np.zeros((nv,exit_horizon),dtype=bool)
        for vi in range(nv):
            s=entry_high_offset_v[vi]+1
            if s<n_bars_per_signal[vi]: fsr[vi,s:n_bars_per_signal[vi]]=True
        fm=np.isfinite(fcol)&fsr
        fhit=((fcol>=ft) if fd=="above" else (fcol<=ft))&fm
        fhb=np.where(fhit,bi,exit_horizon+1); feb=np.min(fhb,axis=1)
        fcap=np.full(nv,-LOSS_ADR,dtype=np.float64); ftrig=feb<exit_horizon+1
        for vi in np.where(ftrig)[0]:
            fb=feb[vi]; ec=close_2d[vi,fb]
            if np.isfinite(ec) and adr_values_v[vi]>0:
                if direction=="short": fcap[vi]=(entry_prices_v[vi]-ec)/adr_values_v[vi]
                else: fcap[vi]=(ec-entry_prices_v[vi])/adr_values_v[vi]
        pe=np.zeros((nv,exit_horizon),dtype=bool); nr=0
        for vi in range(nv):
            eh=entry_high_offset_v[vi]; fb=feb[vi]
            if fb<exit_horizon+1 and eh+2<=fb: pe[vi,eh+1:fb]=True; nr+=1
        bh=np.full(nv,exit_horizon,dtype=np.int32)
        for vi in np.where(ftrig)[0]: bh[vi]=feb[vi]-entry_high_offset_v[vi]
        if nr>=20:
            final_data.append({"name":fn,"direction":fd,"threshold":ft,"exit_bars":feb,"capture":fcap,"triggered":ftrig,"pre_exit_mask":pe,"n_with_room":nr,"bars_held":bh,"expectancy":f["expectancy"]})

    na=len(final_data)
    print(f"  Active final exits (>=20 signals with room): {na}/{n_final}")
    if na==0: print("  No room for trim."); return []

    # Main grind: outer=expressions, inner=final exits
    all_combos=[]; total_tested=0
    for ei in range(ne):
        if (ei+1)%1000==0:
            el=time.time()-t0; r=(ei+1)/el if el>0 else 0
            print(f"    [{ei+1}/{ne}] {r:.0f} expr/s, {len(all_combos):,} combos, {total_tested:,} tested")
        if (ei+1)%2000==0: _check(f"(2stg {ei+1})")
        col=_ecp(fwd_expr_list,valid_indices,ei,exit_horizon)
        ebv=_eeb(entry_high_bar_expr_list,valid_indices,ei)
        ebf=np.isfinite(ebv); tn=filtered_names[ei]
        for fd in final_data:
            fm=np.isfinite(col)&fd["pre_exit_mask"]; fv=col[fm]
            if len(fv)<20: continue
            ths=np.unique(np.percentile(fv,np.linspace(5,95,N_TH)))
            if len(ths)<2: continue
            for th in ths:
                for dl,above in [("above",True),("below",False)]:
                    total_tested+=1
                    hit=((col>=th) if above else (col<=th))&fm
                    hb=np.where(hit,bi,exit_horizon+1); tb=np.min(hb,axis=1)
                    tt=tb<exit_horizon+1
                    ate=ebf&((ebv>=th) if above else (ebv<=th)); tt=tt&(~ate)
                    nt=int(tt.sum())
                    if nt<5: continue
                    for tp in TRIM_PCTS:
                        bl=fd["capture"].copy()
                        for vi in np.where(tt)[0]:
                            tbi=tb[vi]; tc=close_2d[vi,tbi]
                            if np.isfinite(tc) and adr_values_v[vi]>0:
                                if direction=="short": tcap=(entry_prices_v[vi]-tc)/adr_values_v[vi]
                                else: tcap=(tc-entry_prices_v[vi])/adr_values_v[vi]
                                bl[vi]=tp*tcap+(1-tp)*fd["capture"][vi]
                        st=stats_fn(bl,weights_v,fd["triggered"],move_adrs_v,fd["bars_held"])
                        if st is None: continue
                        st["mode"]="2-stage"; st["trim_expr"]=tn; st["trim_direction"]=dl
                        st["trim_threshold"]=round(float(th),6); st["trim_pct"]=tp
                        st["final_expr"]=fd["name"]; st["final_direction"]=fd["direction"]
                        st["final_threshold"]=fd["threshold"]
                        st["n_trimmed"]=nt; st["trim_rate"]=round(nt/nv,4)
                        st["final_exit_expectancy"]=fd["expectancy"]
                        all_combos.append(st)

    el=time.time()-t0
    print(f"\n  2-stage: {el:.1f}s ({el/60:.1f} min), {total_tested:,} tested, {len(all_combos):,} raw")
    _pram("(after 2-stage)")
    all_combos.sort(key=lambda c:c.get("expectancy",float('-inf')),reverse=True)
    seen=set(); deduped=[]
    for c in all_combos:
        k=(c["trim_expr"],c["final_expr"],c["trim_pct"])
        if k not in seen: seen.add(k); deduped.append(c)
    print(f"  After dedup: {len(deduped):,}")
    improved=[c for c in deduped if c["expectancy"]>c["final_exit_expectancy"]]
    print(f"  Combos beating 1-stage: {len(improved)}")
    if improved:
        print(f"\n  Top 10 2-stage (beating 1-stage):")
        print(f"    {'#':<3} {'Trim Expr':<30} {'Dir':<6} {'Trim%':>5} {'Final Expr':<30} {'Exp':>6} {'1stg':>6} {'D':>5} {'TrR':>5}")
        print(f"    {'-'*110}")
        for i,c in enumerate(improved[:10]):
            d=c["expectancy"]-c["final_exit_expectancy"]
            print(f"    {i+1:<3} {c['trim_expr']:<30} {c['trim_direction']:<6} {c['trim_pct']:>5.0%} {c['final_expr']:<30} {c['expectancy']:>6.3f} {c['final_exit_expectancy']:>6.3f} {d:>+5.3f} {c['trim_rate']:>5.1%}")
    else:
        print(f"\n  No 2-stage combo beats 1-stage.")
    return deduped
