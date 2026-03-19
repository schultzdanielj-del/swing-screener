"""
Profit Grinder — Phase 4: TA-Expression-Based Exit Optimization

Brute-forces the expression cache (~12K expressions after boolean exclusion)
testing every expression × threshold × direction against forward expression
values of all winner signals to find optimal exit conditions.

Two distinct forward windows:
  - entry_window: how far from the signal bar the refinement grinder looked
    to find entry_high (read from raw_signal_clusters file). Used to locate
    which bar has the entry_high price.
  - exit_horizon: how far from the signal bar to search for exit conditions
    (default 120 bars). Exit search starts AFTER the entry_high bar.

Weighting:
  - Examples + vetted YES: weight 1.0, hard trigger requirement
  - Vetted NO: excluded entirely
  - Unvetted winners: weighted by entry_candle_score

See PROFIT_GRINDER.md for full spec.

Usage:
    python scripts/profit_grinder.py --setup dtss
    python scripts/profit_grinder.py --setup dtss --exit-horizon 120
"""

import argparse, sys, os, time, json, glob, sqlite3, gc
import numpy as np, pickle
from datetime import datetime, timezone

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")
sys.path.insert(0, REPO_ROOT); sys.path.insert(0, LOCAL_DIR)

EXIT_HORIZON_DEFAULT = 120  # how far from signal bar to search for exit expressions
INITIAL_CAPITAL = 100_000
RISK_PER_TRADE = 0.01
TRADING_DAYS_PER_YEAR = 252
TOP_N_DETAIL = 100
LOSS_ASSUMPTION_ADR = 1.0
N_THRESHOLDS = 100
BOOLEAN_AGG_PREFIXES = ("ct_", "st_", "tir_")
DEDUP_CORR_THRESHOLD = 0.95
DEDUP_TOP_N = 500
SETUP_CONFIGS = {"dtss": {"direction": "short"}, "3-4db": {"direction": "short"}, "htf": {"direction": "long"}}

# ── RAM ──
def get_available_ram_gb():
    try:
        import psutil; return psutil.virtual_memory().available / (1024**3)
    except ImportError:
        try:
            if sys.platform == 'win32':
                import ctypes; k = ctypes.windll.kernel32; u = ctypes.c_ulonglong
                class M(ctypes.Structure):
                    _fields_ = [('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),
                        ('ullTotalPhys',u),('ullAvailPhys',u),('ullTotalPageFile',u),
                        ('ullAvailPageFile',u),('ullTotalVirtual',u),('ullAvailVirtual',u),('ullAvailExtendedVirtual',u)]
                s = M(); s.dwLength = ctypes.sizeof(s); k.GlobalMemoryStatusEx(ctypes.byref(s))
                return s.ullAvailPhys / (1024**3)
        except: pass
    return None
def check_ram(label="", min_gb=2.0):
    a = get_available_ram_gb()
    if a is not None and a < min_gb:
        print(f"\n  ✗ RAM ABORT {label}: {a:.1f} GB avail, need {min_gb:.1f}"); sys.exit(1)
def print_ram(label=""):
    a = get_available_ram_gb()
    if a is not None: print(f"  RAM available: {a:.1f} GB {label}")

# ── Data Loading ──
def load_5yr_cache():
    for n in ("universe_ohlcv_5yr.pkl","universe_ohlcv.pkl"):
        p = os.path.join(CACHE_DIR, n)
        if os.path.exists(p):
            print(f"  Loading 5yr OHLCV from {n}...")
            with open(p,"rb") as f: c = pickle.load(f)
            print(f"  {len(c)} tickers"); return c
    raise FileNotFoundError("No OHLCV cache.")
def find_latest_ev_file(st):
    cs = [os.path.join(CACHE_DIR,f) for f in os.listdir(CACHE_DIR) if f.startswith(f"ev_{st}_") and f.endswith(".json")]
    if not cs: raise FileNotFoundError(f"No EV output for {st}")
    cs.sort(key=os.path.getmtime, reverse=True); return cs[0]
def load_ev_data(st, ef=None):
    p = ef or find_latest_ev_file(st)
    print(f"  Loading EV data from {os.path.basename(p)}...")
    with open(p) as f: d = json.load(f)
    print(f"  {len(d.get('signals',[]))} total signals"); return d, p
def load_entry_scores(st):
    p = os.path.join(CACHE_DIR, f"entry_scores_{st}.json")
    if not os.path.exists(p):
        cs = glob.glob(os.path.join(CACHE_DIR, f"entry_scores_{st}_*.json"))
        if not cs: print(f"  WARNING: No entry scores for {st}"); return {}
        cs.sort(key=os.path.getmtime, reverse=True); p = cs[0]
    print(f"  Loading entry scores from {os.path.basename(p)}...")
    with open(p) as f: d = json.load(f)
    lk = {}
    for s in d.get("scored_signals",[]):
        t,sd,sc = s.get("ticker"), s.get("signal_date",s.get("date")), s.get("entry_candle_score")
        if t and sd and sc is not None: lk[(t,sd)] = sc
    print(f"  {len(lk)} signals with entry_candle_score"); return lk
def load_vetting_decisions(st):
    ek, rk = set(), set()
    if not os.path.exists(DB_PATH): print(f"  WARNING: No DB at {DB_PATH}"); return ek, rk
    try:
        cn = sqlite3.connect(DB_PATH); cn.row_factory = sqlite3.Row
        for r in cn.execute("SELECT ticker,entry_date,chart_date FROM examples WHERE setup_type=?",(st,)):
            ek.add((r["ticker"],r["entry_date"]))
            if r["chart_date"]: ek.add((r["ticker"],r["chart_date"]))
        try:
            for r in cn.execute("SELECT ticker,signal_date FROM rejected_signals WHERE setup_type=?",(st,)):
                rk.add((r["ticker"],r["signal_date"]))
        except sqlite3.OperationalError: pass
        cn.close()
    except Exception as e: print(f"  WARNING: DB error: {e}")
    print(f"  Examples: {len(ek)} keys, Rejected: {len(rk)} keys"); return ek, rk

def load_entry_window(setup_type):
    """Load the entry_window from the raw_signal_clusters file.

    This is how far from the rightmost signal bar the refinement grinder
    looked to find the entry_high price. Must match exactly so the profit
    grinder finds the same bar.
    """
    latest = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if not os.path.exists(latest):
        cs = glob.glob(os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}_*.json"))
        if not cs:
            print(f"  WARNING: No cluster file for {setup_type} — using entry_window=1 (entry candle only)")
            return 1
        cs.sort(key=os.path.getmtime, reverse=True)
        latest = cs[0]
    with open(latest) as f:
        data = json.load(f)
    ew = data.get("forward_window")
    if ew is None or ew < 1:
        print(f"  WARNING: No forward_window in cluster file — using entry_window=1")
        return 1
    print(f"  Entry window from cluster file: {ew} bars ({os.path.basename(latest)})")
    return int(ew)

# ── Signal Population ──
def build_signal_population(ev_data, entry_scores, example_keys, rejected_keys):
    raw = ev_data.get("signals",[])
    signals = []
    counts = {"no_move":0,"no_entry":0,"rejected":0,"examples":0,"vetted_yes":0,"unvetted":0,"no_score":0}
    for sig in raw:
        if sig.get("move_adr") is None: counts["no_move"]+=1; continue
        if sig.get("entry_high") is None or sig.get("adr_at_signal") is None: counts["no_entry"]+=1; continue
        if sig["adr_at_signal"]<=0: counts["no_entry"]+=1; continue
        key=(sig["ticker"],sig["date"])
        if key in rejected_keys: counts["rejected"]+=1; continue
        if sig.get("is_example",False): w,cat=1.0,"example"; counts["examples"]+=1
        else:
            ec=entry_scores.get(key)
            if ec is not None: w,cat=max(float(ec),0.0),"unvetted"; counts["unvetted"]+=1
            else: w,cat=0.0,"unvetted_no_score"; counts["no_score"]+=1
        s=dict(sig); s["weight"]=w; s["weight_category"]=cat; s["entry_candle_score"]=entry_scores.get(key)
        signals.append(s)
    counts["total"]=len(signals); return signals, counts

# ── Expression Filtering ──
def build_expr_col_map(expr_names):
    ecm,fn,ne = [],[],0
    for ci,n in enumerate(expr_names):
        if n.startswith(BOOLEAN_AGG_PREFIXES): ne+=1; continue
        ecm.append((len(ecm),ci)); fn.append(n)
    return ecm, fn, ne

# ── Forward Data Construction ──
def build_forward_data(signals, ohlcv_cache, expr_cache, expr_col_map,
                       direction, exit_horizon, entry_window):
    """Build per-signal forward data with entry_high bar detection.

    Two windows:
      - entry_window: how far from signal_bar+1 to search for the entry_high
        bar (must match the refinement grinder's forward_window).
      - exit_horizon: how far from signal_bar+1 to load forward expression
        data for exit searching.

    For examples, entry_high is the high of the entry candle (bar 0 = signal_bar+1).
    For non-examples, entry_high is the max high within entry_window bars.
    """
    import pandas as pd
    n_signals = len(signals)
    cache_cols = np.array([ci for _,ci in expr_col_map], dtype=np.int32)
    fwd_expr = [None]*n_signals
    fwd_closes = [None]*n_signals
    entry_high_bar_expr = [None]*n_signals
    entry_high_offset = np.full(n_signals, -1, dtype=np.int32)
    entry_prices = np.zeros(n_signals, dtype=np.float64)
    adr_values = np.zeros(n_signals, dtype=np.float64)
    signal_meta = []
    loaded=skipped_ohlcv=skipped_expr=skipped_date=skipped_fwd=skipped_no_ehbar=0
    tg = {}
    for i,sig in enumerate(signals): tg.setdefault(sig["ticker"],[]).append(i)
    for ticker, indices in tg.items():
        df = ohlcv_cache.get(ticker)
        if df is None:
            skipped_ohlcv+=len(indices)
            for i in indices: signal_meta.append({"idx":i,"ticker":ticker,"status":"no_ohlcv"})
            continue
        if not pd.api.types.is_datetime64_any_dtype(df["date"]): df=df.copy(); df["date"]=pd.to_datetime(df["date"])
        df=df.sort_values("date").reset_index(drop=True)
        ds=df["date"].dt.strftime("%Y-%m-%d").values
        d2i={d:i for i,d in enumerate(ds)}
        ca=df["close"].values.astype(np.float64)
        ha=df["high"].values.astype(np.float64)
        ed,edata=expr_cache.get_ticker(ticker)
        if ed is None:
            skipped_expr+=len(indices)
            for i in indices: signal_meta.append({"idx":i,"ticker":ticker,"status":"no_expr"})
            continue
        edm={str(d)[:10]:i for i,d in enumerate(ed)}
        for i in indices:
            sig=signals[i]; sd=sig["date"]; oi=d2i.get(sd); ei=edm.get(sd)
            if oi is None or ei is None:
                skipped_date+=1; signal_meta.append({"idx":i,"ticker":ticker,"status":"no_date","date":sd}); continue
            entry_prices[i]=sig["entry_high"]
            adr_values[i]=sig["adr_at_signal"]
            # Forward window for exit search (exit_horizon bars from signal_bar+1)
            nf=min(min(len(df)-oi-1,len(edata)-ei-1),exit_horizon)
            if nf<1:
                skipped_fwd+=1; signal_meta.append({"idx":i,"ticker":ticker,"status":"no_fwd","date":sd}); continue

            fwd_closes[i]=ca[oi+1:oi+1+nf].copy()
            fwd_expr[i]=edata[ei+1:ei+1+nf][:,cache_cols].copy()

            # Find which bar in the forward window has the entry_high price.
            # Only search within entry_window bars (matching the refinement grinder).
            eh_price = sig["entry_high"]
            eh_search_end = min(entry_window, nf)  # don't search beyond available bars
            fwd_highs = ha[oi+1:oi+1+eh_search_end]
            eh_diffs = np.abs(fwd_highs - eh_price)
            eh_bar = int(np.argmin(eh_diffs))
            # Verify it's actually close (within 0.01 or 0.1% of price)
            if eh_diffs[eh_bar] > max(0.01, eh_price * 0.001):
                eh_bar = 0
                skipped_no_ehbar += 1

            entry_high_offset[i] = eh_bar
            entry_high_bar_expr[i] = fwd_expr[i][eh_bar, :].copy()

            fd=ds[oi+1:oi+1+nf].tolist()
            signal_meta.append({"idx":i,"ticker":ticker,"signal_date":sd,
                "entry_price":float(entry_prices[i]),"adr":float(adr_values[i]),
                "entry_high_bar_offset":int(eh_bar),
                "classification":sig.get("classification"),"is_example":sig.get("is_example",False),
                "quality_score":sig.get("quality_score",0),"move_adr":sig.get("move_adr"),
                "killed_at_depth":sig.get("killed_at_depth"),"weight":sig["weight"],
                "weight_category":sig["weight_category"],"entry_candle_score":sig.get("entry_candle_score"),
                "n_forward_bars":nf,"fwd_dates":fd,"status":"ok"})
            loaded+=1
    vm=np.array([fwd_expr[i] is not None for i in range(n_signals)])
    stats={"loaded":loaded,"skipped_ohlcv":skipped_ohlcv,"skipped_expr":skipped_expr,
           "skipped_date":skipped_date,"skipped_fwd":skipped_fwd,
           "skipped_no_ehbar":skipped_no_ehbar,"n_valid":int(vm.sum())}
    return fwd_expr, fwd_closes, entry_high_bar_expr, entry_high_offset, entry_prices, adr_values, signal_meta, vm, stats

def extract_column_padded(fwd_expr_list, valid_indices, expr_col, exit_horizon):
    n=len(valid_indices)
    c=np.full((n,exit_horizon),np.nan,dtype=np.float32)
    for vi,si in enumerate(valid_indices):
        fe=fwd_expr_list[si]; c[vi,:fe.shape[0]]=fe[:,expr_col]
    return c

def extract_entry_high_bar_column(entry_high_bar_expr_list, valid_indices, expr_col):
    n=len(valid_indices)
    vals=np.full(n,np.nan,dtype=np.float32)
    for vi,si in enumerate(valid_indices):
        eb=entry_high_bar_expr_list[si]
        if eb is not None: vals[vi]=eb[expr_col]
    return vals

# ── Weighted Stats ──
def compute_weighted_stats(captured_adr, weights, triggered, move_adrs_actual, n_bars_held):
    n=len(captured_adr)
    if n<2: return None
    w=weights.copy(); ws=w.sum()
    if ws<1e-10: return None
    iw=captured_adr>0; il=~iw
    wr=float(np.sum(w[iw])/ws)
    exp=float(np.sum(captured_adr*w)/ws)
    wv=np.sum(w*(captured_adr-exp)**2)/ws; wstd=float(np.sqrt(max(wv,0.0)))
    ne=(ws**2)/np.sum(w**2) if np.sum(w**2)>0 else 1.0
    sqn=float(np.sqrt(ne)*exp/wstd) if wstd>0 else 0.0
    gw=float(np.sum(captured_adr[iw]*w[iw])) if iw.any() else 0.0
    gl=float(np.abs(np.sum(captured_adr[il]*w[il]))) if il.any() else 0.001
    pf=gw/gl if gl>0 else 999.0
    def _wm(a,m,wt):
        if not m.any(): return 0.0
        s=wt[m].sum(); return float(np.sum(a[m]*wt[m])/s) if s>0 else 0.0
    aw=_wm(captured_adr,iw,w); al=_wm(captured_adr,il,w)
    pr=abs(aw/al) if al!=0 else 999.0
    wa=captured_adr[iw]; la=captured_adr[il]
    eq=_bwec(captured_adr,w,INITIAL_CAPITAL,RISK_PER_TRADE)
    pk=np.maximum.accumulate(eq); dd=np.where(pk>0,(pk-eq)/pk,0.0)
    mdd=float(np.max(dd)); add=float(np.mean(dd[dd>0])) if (dd>0).any() else 0.0
    tb=int(np.sum(n_bars_held)); ab=float(np.mean(n_bars_held))
    yr=tb/TRADING_DAYS_PER_YEAR if tb>0 else 1.0
    tr=eq[-1]/eq[0] if eq[0]>0 else 1.0
    cagr=float((tr**(1/yr)-1)) if yr>0 and tr>0 else 0.0
    tpy=TRADING_DAYS_PER_YEAR/ab if ab>0 else 1.0
    ar=exp*tpy; astd=wstd*np.sqrt(tpy)
    sh=float(ar/astd) if astd>0 else 0.0
    ds=captured_adr[captured_adr<0]
    dstd=float(np.std(ds,ddof=1)) if len(ds)>1 else 1.0
    so=float(ar/(dstd*np.sqrt(tpy))) if dstd>0 else 0.0
    ca=float(cagr/mdd) if mdd>0 else 999.0
    tm=triggered&(move_adrs_actual>0)
    if tm.any():
        ce=captured_adr[tm]/move_adrs_actual[tm]
        mc,fc,mnc=float(np.median(ce)),float(np.min(ce)),float(np.mean(ce))
        sc=float(np.std(ce,ddof=1)) if tm.sum()>1 else 0.0
    else: mc=fc=mnc=sc=0.0
    tbars=n_bars_held[triggered]
    if len(tbars)>0:
        bmi,bmd,bmx=int(np.min(tbars)),int(np.median(tbars)),int(np.max(tbars))
        bstd=float(np.std(tbars,ddof=1)) if len(tbars)>1 else 0.0
    else: bmi=bmd=bmx=0; bstd=0.0
    return {"n_signals":n,"n_triggered":int(triggered.sum()),"trigger_rate":round(triggered.sum()/n,4),
        "n_winners":int(iw.sum()),"n_losers":int(il.sum()),"win_rate":round(wr,4),
        "expectancy":round(exp,4),"sqn":round(sqn,4),
        "profit_factor":round(min(pf,999.0),4),"payoff_ratio":round(min(pr,999.0),4),
        "avg_win_adr":round(aw,4),"avg_loss_adr":round(al,4),
        "median_win_adr":round(float(np.median(wa)),4) if len(wa)>0 else 0.0,
        "median_loss_adr":round(float(np.median(la)),4) if len(la)>0 else 0.0,
        "best_win_adr":round(float(np.max(wa)),4) if len(wa)>0 else 0.0,
        "worst_loss_adr":round(float(np.min(la)),4) if len(la)>0 else 0.0,
        "std_adr":round(wstd,4),
        "max_consec_winners":_maxc(iw),"max_consec_losers":_maxc(il),
        "avg_bars_winners":round(float(np.mean(n_bars_held[iw])),1) if iw.any() else 0.0,
        "avg_bars_losers":round(float(np.mean(n_bars_held[il])),1) if il.any() else 0.0,
        "avg_bars_all":round(ab,1),
        "bars_held_min":bmi,"bars_held_median":bmd,"bars_held_max":bmx,"bars_held_std":round(bstd,1),
        "max_drawdown":round(mdd,4),"avg_drawdown":round(add,4),"max_dd_duration_trades":_mdd(eq),
        "cagr":round(cagr,4),"sharpe":round(sh,4),"sortino":round(so,4),"calmar":round(min(ca,999.0),4),
        "final_equity":round(float(eq[-1]),2),
        "total_return_pct":round(float((eq[-1]/eq[0]-1)*100),2),
        "median_capture_eff":round(mc,4),"floor_capture_eff":round(fc,4),
        "mean_capture_eff":round(mnc,4),"std_capture_eff":round(sc,4)}

def _maxc(ba):
    mx=cur=0
    for v in ba:
        if v: cur+=1; mx=max(mx,cur)
        else: cur=0
    return mx
def _bwec(ca, w, cap, risk):
    n=len(ca); eq=np.zeros(n+1); eq[0]=cap
    for i in range(n):
        eq[i+1]=eq[i]+eq[i]*risk*ca[i]*w[i]
        if eq[i+1]<=0: eq[i+1:]=0; break
    return eq
def _mdd(eq):
    pk=eq[0]; mx=cur=0
    for v in eq[1:]:
        if v>=pk: pk=v; cur=0
        else: cur+=1; mx=max(mx,cur)
    return mx

# ── 1-Stage Expression Grind ──
def grind_1stage(fwd_expr_list, fwd_close_list, entry_high_bar_expr_list,
                 entry_high_offset_v, valid_indices,
                 entry_prices_v, adr_values_v, weights_v, is_hard_gate_v,
                 move_adrs_v, n_bars_per_signal, filtered_names, direction, exit_horizon):
    nv=len(valid_indices); ne=len(filtered_names)
    print(f"\n  ── 1-STAGE EXPRESSION GRIND ──")
    print(f"  {nv} signals × {ne} expressions × ~{N_THRESHOLDS} thresholds × 2 directions")
    print(f"  Hard gate signals: {int(is_hard_gate_v.sum())}")
    print(f"  Entry-high bar detection: ON (exit search starts after entry_high bar)")
    print(f"  Already-true-at-entry filter: ON (checks entry_high bar)")
    print_ram("(before grind)")
    t0=time.time(); candidates=[]; tested=0; hgf=0; atef=0

    cl2d=np.full((nv,exit_horizon),np.nan,dtype=np.float64)
    for vi,si in enumerate(valid_indices):
        fc=fwd_close_list[si]; cl2d[vi,:len(fc)]=fc

    search_valid=np.zeros((nv,exit_horizon),dtype=bool)
    for vi in range(nv):
        eh_off = entry_high_offset_v[vi]
        n_bars = n_bars_per_signal[vi]
        start = eh_off + 1
        if start < n_bars:
            search_valid[vi, start:n_bars] = True

    bi=np.arange(exit_horizon)[np.newaxis,:]

    eh_offsets = entry_high_offset_v
    print(f"  Entry-high bar offsets: min={eh_offsets.min()} med={int(np.median(eh_offsets))} "
          f"max={eh_offsets.max()} (0=entry candle)")
    searchable = search_valid.sum(axis=1)
    print(f"  Searchable bars after entry: min={int(searchable.min())} "
          f"med={int(np.median(searchable))} max={int(searchable.max())}")

    for ei in range(ne):
        if (ei+1)%1000==0:
            el=time.time()-t0; r=(ei+1)/el if el>0 else 0
            print(f"    [{ei+1}/{ne}] {r:.0f} expr/s, {len(candidates)} cands, "
                  f"{tested:,} tested, {hgf:,} gate, {atef:,} already-true")
        if (ei+1)%2000==0: check_ram(f"(at expr {ei+1})",min_gb=1.0)

        col=extract_column_padded(fwd_expr_list,valid_indices,ei,exit_horizon)
        fm=np.isfinite(col)&search_valid
        fv=col[fm]
        if len(fv)<nv: continue
        pcts=np.linspace(5,95,N_THRESHOLDS)
        ths=np.unique(np.percentile(fv,pcts))
        if len(ths)<2: continue

        eb_vals=extract_entry_high_bar_column(entry_high_bar_expr_list,valid_indices,ei)
        eb_finite=np.isfinite(eb_vals)

        en=filtered_names[ei]
        for th in ths:
            for dl,above in [("above",True),("below",False)]:
                tested+=1
                if above: hit=(col>=th)&fm
                else: hit=(col<=th)&fm

                hb=np.where(hit,bi,exit_horizon+1)
                fb=np.min(hb,axis=1)
                triggered=fb<exit_horizon+1

                if above: ate=eb_finite&(eb_vals>=th)
                else: ate=eb_finite&(eb_vals<=th)
                triggered=triggered&(~ate)

                if not triggered[is_hard_gate_v].all():
                    if ate[is_hard_gate_v].any(): atef+=1
                    else: hgf+=1
                    continue

                ca=np.full(nv,-LOSS_ASSUMPTION_ADR,dtype=np.float64)
                bh=np.full(nv,exit_horizon,dtype=np.int32)
                for vi in np.where(triggered)[0]:
                    f=fb[vi]; ec=cl2d[vi,f]
                    if np.isfinite(ec) and adr_values_v[vi]>0:
                        if direction=="short": ca[vi]=(entry_prices_v[vi]-ec)/adr_values_v[vi]
                        else: ca[vi]=(ec-entry_prices_v[vi])/adr_values_v[vi]
                    bh[vi]=f - entry_high_offset_v[vi]

                st=compute_weighted_stats(ca,weights_v,triggered,move_adrs_v,bh)
                if st is None: continue
                st["expr_name"]=en; st["direction"]=dl; st["threshold"]=round(float(th),6)
                st["n_already_true"]=int(ate.sum())
                candidates.append((st,fb.astype(np.int16).copy()))

    el=time.time()-t0
    print(f"\n  Raw grind complete:")
    print(f"    Time: {el:.1f}s ({el/60:.1f} min)")
    print(f"    Tested: {tested:,}")
    print(f"    Hard gate fails: {hgf:,}")
    print(f"    Already-true-at-entry fails: {atef:,}")
    print(f"    Raw candidates: {len(candidates):,}")
    print_ram("(after grind)")
    return dedup_candidates(candidates, exit_horizon)

# ── Dedup ──
def dedup_candidates(candidates, exit_horizon):
    if len(candidates)<2: return [c[0] for c in candidates]
    print(f"\n  ── CANDIDATE DEDUP ──")
    t0=time.time(); nr=len(candidates)
    eb={}
    for st,ex in candidates:
        n=st["expr_name"]
        if n not in eb or st["expectancy"]>eb[n][0]["expectancy"]: eb[n]=(st,ex)
    p1=list(eb.values()); n1=len(p1)
    print(f"  Pass 1 (best/expression): {nr:,} → {n1:,}")
    p1.sort(key=lambda x:x[0]["expectancy"],reverse=True)
    tn=p1[:DEDUP_TOP_N]; rest=p1[DEDUP_TOP_N:]
    if len(tn)<2:
        print(f"  Pass 2: skipped"); return [c[0] for c in p1]
    nc=len(tn); ns=len(tn[0][1])
    em=np.zeros((nc,ns),dtype=np.float64)
    for ci,(st,ex) in enumerate(tn): em[ci,:]=ex.astype(np.float64)
    kept=[]; kr=[]
    for ci in range(nc):
        row=em[ci]; dom=False
        for k in kr:
            bv=(row<exit_horizon+1)&(k<exit_horizon+1); nv=int(bv.sum())
            if nv<50: continue
            rv,kv=row[bv],k[bv]
            if np.std(rv)<1e-10 or np.std(kv)<1e-10: dom=True; break
            if abs(np.corrcoef(rv,kv)[0,1])>=DEDUP_CORR_THRESHOLD: dom=True; break
        if not dom: kept.append(tn[ci]); kr.append(row)
    n2=len(kept); nd=nc-n2; el=time.time()-t0
    print(f"  Pass 2 (exit-bar corr, top {DEDUP_TOP_N}): {nc} → {n2} ({nd} dupes)")
    print(f"  Dedup: {el:.1f}s")
    result=[c[0] for c in kept]+[c[0] for c in rest]
    result.sort(key=lambda c:c.get("expectancy",float('-inf')),reverse=True)
    print(f"  Final: {len(result):,}"); return result

# ── Main ──
def main():
    pa=argparse.ArgumentParser(description="Profit Grinder — Phase 4")
    pa.add_argument("--setup",default="dtss"); pa.add_argument("--direction",default=None)
    pa.add_argument("--ev-file",default=None)
    pa.add_argument("--exit-horizon",type=int,default=EXIT_HORIZON_DEFAULT,
                    help="How far from signal bar to search for exit expressions (default: 120)")
    args=pa.parse_args()
    setup=args.setup; cfg=SETUP_CONFIGS.get(setup,{"direction":"short"}); direction=args.direction or cfg["direction"]
    exit_horizon=args.exit_horizon

    print(f"\n{'='*70}\n  PROFIT GRINDER — Phase 4 Exit Optimization\n{'='*70}")
    print(f"  Setup: {setup.upper()}, Direction: {direction}")
    print(f"  Exit horizon: {exit_horizon} bars (how far to search for exits)")
    print(f"  Loss: {LOSS_ASSUMPTION_ADR} ADR, Thresholds: {N_THRESHOLDS}")
    print_ram("(startup)"); check_ram("(startup)",min_gb=4.0); t0=time.time()

    print(f"\n  ── LOADING DATA ──")
    ev_data,ev_path=load_ev_data(setup,args.ev_file)
    entry_scores=load_entry_scores(setup)
    example_keys,rejected_keys=load_vetting_decisions(setup)
    entry_window=load_entry_window(setup)

    print(f"\n  ── BUILDING POPULATION ──")
    signals,counts=build_signal_population(ev_data,entry_scores,example_keys,rejected_keys)
    print(f"\n  Population: {counts['total']} signals")
    print(f"    Examples: {counts['examples']}  Unvetted: {counts['unvetted']}  "
          f"Rejected: {counts['rejected']}  No score: {counts['no_score']}")
    if counts["no_score"]>0: print(f"  ⚠ {counts['no_score']} missing entry_candle_score")
    w=np.array([s["weight"] for s in signals])
    print(f"  Weights: min={w.min():.4f} med={np.median(w):.4f} max={w.max():.4f} sum={w.sum():.1f}")
    hg=sum(1 for s in signals if s["weight_category"] in ("example","vetted_yes"))
    print(f"  Hard gate: {hg}")
    if counts["total"]<5: print("  ERROR: <5 signals"); sys.exit(1)
    print(f"\n  ⚠ Winner-only population. SQN/WR inflated. Use expectancy + capture_eff for ranking.")

    print(f"\n  ── EXPRESSION CACHE ──")
    from expr_cache_builder import ExprSeriesCache
    ec=ExprSeriesCache()
    if not ec.is_valid(): print("  ERROR: expr cache invalid"); sys.exit(1)
    ecm,fn,ne=build_expr_col_map(ec.expr_names); nf=len(ecm)
    print(f"  {len(ec.expr_names)} total, {ne} boolean excluded, {nf} for search")

    print(f"\n  ── FORWARD MATRIX CONSTRUCTION ──")
    print(f"  Entry window: {entry_window} bars (from cluster file — where entry_high was computed)")
    print(f"  Exit horizon: {exit_horizon} bars (how far to search for exits)")
    check_ram("(pre-OHLCV)",min_gb=3.0); oc=load_5yr_cache(); print_ram("(OHLCV loaded)")
    fwd_expr,fwd_closes,entry_high_bar_expr,entry_high_offset,entry_prices,adr_values,signal_meta,valid_mask,bs=\
        build_forward_data(signals,oc,ec,ecm,direction,exit_horizon,entry_window)
    del oc; gc.collect()
    print(f"  Loaded: {bs['loaded']}  Valid: {bs['n_valid']}")
    if bs.get('skipped_no_ehbar',0)>0:
        print(f"  ⚠ {bs['skipped_no_ehbar']} signals: entry_high not found within entry_window (bar 0 fallback)")
    if bs['loaded']<counts['total']:
        print(f"  Skipped: ohlcv={bs['skipped_ohlcv']} expr={bs['skipped_expr']} "
              f"date={bs['skipped_date']} fwd={bs['skipped_fwd']}")
    exl=sum(1 for m in signal_meta if m.get("status")=="ok" and m.get("is_example"))
    if exl<counts["examples"]: print(f"  ✗ FAIL: {exl}/{counts['examples']} examples"); sys.exit(1)
    print(f"  ✓ All {counts['examples']} examples loaded")
    tfb=sum(fwd_expr[si].nbytes for si in range(len(signals)) if fwd_expr[si] is not None)
    print(f"  Forward data: {tfb/1e9:.2f} GB across {bs['n_valid']} arrays")
    print_ram("(OHLCV freed)"); check_ram("(pre-grind)",min_gb=2.0)

    vi=np.where(valid_mask)[0]
    sd=[signals[si]["date"] for si in vi]
    do=np.argsort(sd); vi=vi[do]
    wv=np.array([signals[si]["weight"] for si in vi])
    epv=entry_prices[vi]; av=adr_values[vi]
    mv=np.array([signals[si].get("move_adr",0) or 0 for si in vi])
    ihg=np.array([signals[si]["weight_category"] in ("example","vetted_yes") for si in vi])
    nbp=np.array([fwd_expr[si].shape[0] for si in vi],dtype=np.int32)
    eho=entry_high_offset[vi]

    cands=grind_1stage(fwd_expr,fwd_closes,entry_high_bar_expr,eho,vi,
                       epv,av,wv,ihg,mv,nbp,fn,direction,exit_horizon)

    if cands:
        t=cands[0]
        print(f"\n  Top candidate (by expectancy after dedup):")
        print(f"    {t['expr_name']} {t['direction']} {t['threshold']}")
        print(f"    Exp={t['expectancy']:.3f}  SQN={t['sqn']:.3f}  WR={t['win_rate']:.1%}  PF={t['profit_factor']:.2f}")
        print(f"    Triggered: {t['n_triggered']}/{t['n_signals']}  Already-true: {t.get('n_already_true',0)}")
        print(f"    Capture: med={t['median_capture_eff']:.2f}±{t['std_capture_eff']:.2f} floor={t['floor_capture_eff']:.2f}")
        print(f"    Bars after entry: min={t['bars_held_min']} med={t['bars_held_median']} max={t['bars_held_max']} std={t['bars_held_std']:.1f}")
        print(f"    Equity: ${t['final_equity']:,.0f}  CAGR={t['cagr']:.1%}  MaxDD={t['max_drawdown']:.1%}")

    if len(cands)>=10:
        print(f"\n  Top 10 by expectancy (after dedup):")
        print(f"    {'#':<3} {'Expression':<40} {'Dir':<6} {'Thresh':>8} {'Exp':>6} "
              f"{'CapM':>5} {'Cap±':>5} {'BrM':>4} {'Br±':>5} {'Trg%':>5} {'ATE':>4}")
        print(f"    {'-'*100}")
        for i,c in enumerate(cands[:10]):
            print(f"    {i+1:<3} {c['expr_name']:<40} {c['direction']:<6} "
                  f"{c['threshold']:>8.4f} {c['expectancy']:>6.3f} "
                  f"{c['median_capture_eff']:>5.2f} {c['std_capture_eff']:>5.2f} "
                  f"{c['bars_held_median']:>4} {c['bars_held_std']:>5.1f} "
                  f"{c['trigger_rate']:>5.1%} {c.get('n_already_true',0):>4}")

    el=time.time()-t0
    print(f"\n  {'='*50}")
    print(f"  COMPLETE ({el:.1f}s / {el/60:.1f} min)")
    print(f"  {'='*50}")
    print(f"  Signals: {counts['total']}  Expressions: {nf}  Candidates: {len(cands)}")
    print(f"  Entry window: {entry_window} bars  Exit horizon: {exit_horizon} bars")
    print(f"  EV source: {os.path.basename(ev_path)}")
    print_ram("(final)"); print(f"  {'='*50}")

if __name__=="__main__": main()
