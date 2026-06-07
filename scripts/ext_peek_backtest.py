"""Extension Peek backtest (faithful, 2020->now).

Replicates the LIVE Extension Peek setup exactly:
  - ext50 on a 20-bar ADR (snapshot builder's _compute_ext50_series)
  - cascade_at + the strict _has_line_break re-filter (no broken trendlines)
  - peek = a clean descending line that price was at/below yesterday (t-1)
    and has crossed above today (t)

Entry ruleset (all on the signal/entry bar t, 2020-01-02 onward):
  1. peek fires (above)
  2. close > SMA200
  3. SMA50 > SMA200
  4. SMA10 > SMA20
  5. SMA10 > SMA50 and SMA20 > SMA50
  6. signal candle range < 1.1 * ADR20
  7. ext < 4.0   (close <= 4 ADR above SMA50 -- the peek's own metric)
  8. SPY SMA10 > SMA20, both rising (each > value 3 bars ago)
  9. VIX close < 20

Outcome per firing:
  - entry  = close of signal bar
  - stop   = signal bar low (loss if a later bar's low < signal low)
  - window = 60 bars after entry (truncated near end of data)
  - MFE    = max(high - entry close) over alive bars BEFORE any breach
             (breach bar's high not credited); reported in R / ADR / %
  - win = not stopped within 60 bars

Speed: cheap gates first; cascade only at t-1 of each survivor, deduped.
RAM:   parent holds cache; workers get one DataFrame each (Windows spawn).
Read-only on all caches. Writes one results JSON in full mode.
"""
import os, sys, json, time, pickle, argparse
from datetime import datetime, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
sys.path.insert(0, os.path.join(ROOT, "local_runner"))
sys.path.insert(0, ROOT)
CACHE = os.path.join(ROOT, "local_runner", "cache")

from vectorized_indicators import sma_2d  # noqa: E402

# ---- ruleset constants ----
START = "2020-01-02"
EXT_CAP = 4.0
CANDLE_CAP = 1.1
WINDOW = 60
SPY_SLOPE_LOOKBACK = 3

# Globals set per worker (avoid re-sending regime each task)
_REGIME = None

def _init_worker(regime):
    global _REGIME
    _REGIME = regime


# ---- indicator helpers (mirror snapshot builder) ----

def _sma(c, n):
    return sma_2d(c.reshape(1, -1), n)[0]

def _adr20(h, l):
    n = len(h)
    adr = np.full(n, np.nan)
    for i in range(19, n):
        wh, wl = h[i-19:i+1], l[i-19:i+1]
        m = (~np.isnan(wh)) & (~np.isnan(wl)) & (wl > 0)
        if m.sum() >= 1:
            adr[i] = float(np.mean((wh[m]/wl[m] - 1.0) * 100.0))
    return adr

def _ext50(c, sma50, adr):
    pct = (c - sma50) / np.where(sma50 > 0, sma50, np.nan) * 100.0
    return pct / np.where(adr > 0, adr, np.nan)


# ---- faithful clean-descending set at a bar (mirrors snapshot builder) ----

def _clean_descending(ext, asof_bar, levels_scalar):
    from scripts.ext50_trendlines import cascade_at, _has_line_break
    snap = cascade_at(ext, asof_bar, levels_scalar)
    out = []
    for c in (snap.get("all_candidates") or []):
        if c["anchor_type"] == "peak_anchored":
            if _has_line_break(ext, c["i0"], c["v0"], c["i1"], c["v1"], asof_bar):
                continue
            out.append(c)
    out.sort(key=lambda c: abs(c["signed_dist"]))
    return out[:3]


# ---- per-ticker worker ----

def _process_ticker(args):
    ticker, df = args
    try:
        if df is None or len(df) < 260:
            return ticker, [], None, 0, 0
        d = df["date"].astype(str).str[:10].values
        c = df["close"].values.astype(np.float64)
        h = df["high"].values.astype(np.float64)
        l = df["low"].values.astype(np.float64)
        n = len(c)

        s10, s20, s50, s200 = _sma(c,10), _sma(c,20), _sma(c,50), _sma(c,200)
        adr = _adr20(h, l)
        ext = _ext50(c, s50, adr)
        crange = (h / np.where(l > 0, l, np.nan) - 1.0) * 100.0

        # cheap gates -> survivor bar indices (entry bars)
        survivors = []
        for i in range(200, n):
            if d[i] < START: continue
            if not _REGIME.get(d[i], False): continue
            if np.isnan(s200[i]) or np.isnan(adr[i]) or np.isnan(ext[i]): continue
            if not (c[i] > s200[i]): continue
            if not (s50[i] > s200[i]): continue
            if not (s10[i] > s20[i]): continue
            if not (s10[i] > s50[i] and s20[i] > s50[i]): continue
            if not (crange[i] < CANDLE_CAP * adr[i]): continue
            if not (ext[i] < EXT_CAP): continue
            survivors.append(i)

        if not survivors:
            return ticker, [], None, 0, 0

        # cascade only at t-1 of each survivor, deduped
        from scripts.reversal_profile import compute_all_reversal_profile_series
        rp = compute_all_reversal_profile_series(ext)
        lv = {k: rp.get(k) for k in ("upside_1","upside_2","downside_1","downside_2","chop_upper")}
        need = sorted(set(t-1 for t in survivors))
        lines_at = {}
        for b in need:
            ls = {k: (float(lv[k][b]) if lv[k] is not None and b < len(lv[k]) and not np.isnan(lv[k][b]) else float("nan")) for k in lv}
            lines_at[b] = _clean_descending(ext, b, ls)
        n_casc = len(need)

        trades = []
        for t in survivors:
            lines = lines_at.get(t-1) or []
            hit = None
            for slot, u in enumerate(lines, 1):   # tightest first
                proj_t = u["v1"] + u["slope"] * (t - u["i1"])
                today_sd = proj_t - ext[t]
                yest_sd = u["signed_dist"]
                if today_sd < 0 and yest_sd >= 0:
                    hit = (slot, today_sd, yest_sd); break
            if hit is None:
                continue
            slot, today_sd, yest_sd = hit

            # ---- outcome: MFE before stop, 60-bar window ----
            entry_close = c[t]; sig_low = l[t]
            risk = entry_close - sig_low
            if risk <= 0:
                continue
            end = min(t + WINDOW, n - 1)
            best_high = -np.inf; stopped = False; breach = None
            for j in range(t+1, end+1):
                if l[j] < sig_low:
                    stopped = True; breach = j; break
                if h[j] > best_high:
                    best_high = h[j]
            mfe_abs = 0.0 if best_high == -np.inf else max(0.0, best_high - entry_close)
            adr_price = entry_close * adr[t] / 100.0
            trades.append({
                "ticker": ticker, "date": d[t], "slot": slot,
                "entry_close": round(entry_close, 4), "sig_low": round(sig_low, 4),
                "risk": round(risk, 4), "ext_at_entry": round(float(ext[t]), 3),
                "mfe_abs": round(mfe_abs, 4),
                "mfe_R": round(mfe_abs / risk, 3),
                "mfe_adr": round(mfe_abs / adr_price, 3) if adr_price > 0 else None,
                "mfe_pct": round(mfe_abs / entry_close * 100.0, 3),
                "stopped": bool(stopped),
                "bars_to_breach": (breach - t) if breach is not None else None,
                "window_trunc": bool(t + WINDOW > n - 1),
            })
        return ticker, trades, None, len(survivors), n_casc
    except Exception as exc:
        return ticker, [], repr(exc), 0, 0


# ---- regime gate from market cache ----

def _build_regime():
    with open(os.path.join(CACHE, "market_ohlcv.pkl"), "rb") as f:
        M = pickle.load(f)
    spy = M["SPY"]; sc = spy["close"].values.astype(float)
    sd = spy["date"].astype(str).str[:10].values
    s10 = _sma(sc, 10); s20 = _sma(sc, 20)
    k = SPY_SLOPE_LOOKBACK
    r10 = np.concatenate([[False]*k, s10[k:] > s10[:-k]])
    r20 = np.concatenate([[False]*k, s20[k:] > s20[:-k]])
    spy_ok = (s10 > s20) & r10 & r20
    spy_gate = {dd: bool(x) for dd, x in zip(sd, spy_ok)}
    vix = M["VIX.INDX"]
    vix_gate = {dd: float(v) < 20.0 for dd, v in zip(vix["date"].astype(str).str[:10].values, vix["close"].values)}
    return {dd: (spy_gate.get(dd, False) and vix_gate.get(dd, False)) for dd in spy_gate if dd >= START}


def _load_universe():
    print("cache:", CACHE)
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        U = pickle.load(f)
    print("universe tickers:", len(U))
    assert len(U) > 11200, "ticker count too low -- STOP"
    return U


# ---- modes ----

def mode_validate(n):
    """Confirm faithful replication: my clean-descending set at the snapshot's
    asof bar must match the live snapshot's stored u-line signed_dists."""
    U = _load_universe()
    snap = json.load(open(os.path.join(CACHE, "ext50_trendline_snapshots.json"), encoding="utf-8"))
    snaps = snap["tickers"]
    tickers = [t for t in U if t in snaps and snaps[t].get("u")]
    tickers = sorted(tickers)[::max(1, len(tickers)//n)][:n]
    ok = bad = 0
    for tk in tickers:
        df = U[tk]; sd = snaps[tk]["asof_date"]
        d = df["date"].astype(str).str[:10].values
        idx = np.where(d == sd)[0]
        if len(idx) == 0:
            continue
        b = int(idx[0])
        c = df["close"].values.astype(np.float64); h = df["high"].values.astype(np.float64); l = df["low"].values.astype(np.float64)
        ext = _ext50(c, _sma(c,50), _adr20(h,l))
        from scripts.reversal_profile import compute_all_reversal_profile_series
        rp = compute_all_reversal_profile_series(ext)
        ls = {kk: (float(rp[kk][b]) if rp.get(kk) is not None and not np.isnan(rp[kk][b]) else float("nan")) for kk in ("upside_1","upside_2","downside_1","downside_2","chop_upper")}
        mine = _clean_descending(ext, b, ls)
        live = snaps[tk]["u"]
        my_sd = [round(x["signed_dist"], 3) for x in mine]
        lv_sd = [round(x["signed_dist"], 3) for x in live]
        if my_sd == lv_sd:
            ok += 1
        else:
            bad += 1
            if bad <= 8:
                print(f"  MISMATCH {tk} @ {sd}: mine={my_sd} live={lv_sd}")
    print(f"\nvalidate: {ok} match / {bad} mismatch (of {ok+bad} compared)")


def mode_run(n, workers, full):
    U = _load_universe()
    regime = _build_regime()
    on = sum(regime.values())
    print(f"regime 2020+: {on}/{len(regime)} days on")
    items = [(t, U[t]) for t in sorted(U.keys())]
    if not full:
        items = items[::max(1, len(items)//n)][:n]
    print(f"processing {len(items)} tickers with {workers} workers...")

    all_trades = []; errors = {}; t0 = time.time(); done = 0
    tot_surv = tot_casc = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(regime,)) as exe:
        futs = [exe.submit(_process_ticker, it) for it in items]
        for fut in as_completed(futs):
            tk, trades, err, nsurv, ncasc = fut.result()
            done += 1; tot_surv += nsurv; tot_casc += ncasc
            if err: errors[tk] = err
            if trades: all_trades.extend(trades)
            if done in (50, 100) or done % 200 == 0:
                el = time.time()-t0; rate = done/el if el>0 else 0
                eta_s = (len(items)-done)/rate if rate>0 else 0
                print(f"  {done}/{len(items)}  trades={len(all_trades)}  casc={tot_casc}  elapsed={el/60:.1f}m  rate={rate:.2f}/s  ETA={eta_s/3600:.1f}h")
    el = time.time()-t0
    print(f"\ndone {len(items)} tickers in {el:.0f}s | survivors={tot_surv} cascades={tot_casc} trades={len(all_trades)} errors={len(errors)}")
    _summary(all_trades)

    if full:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = os.path.join(CACHE, f"ext_peek_backtest_{stamp}.json")
        json.dump({
            "built_at": datetime.now(timezone.utc).isoformat(),
            "window_bars": WINDOW, "ext_cap": EXT_CAP, "candle_cap": CANDLE_CAP,
            "start": START, "n_tickers": len(items), "n_trades": len(all_trades),
            "elapsed_s": round(el, 1), "trades": all_trades,
        }, open(out, "w"), separators=(",", ":"))
        print(f"wrote {out} ({os.path.getsize(out)/1e6:.1f} MB)")


def _summary(trades):
    if not trades:
        print("no trades"); return
    R = np.array([t["mfe_R"] for t in trades])
    stopped = np.array([t["stopped"] for t in trades])
    print(f"\n=== {len(trades)} firings ===")
    print(f"stopped (loss): {stopped.sum()} ({100*stopped.mean():.1f}%) | survivors: {(~stopped).sum()}")
    print(f"MFE in R: mean={R.mean():.2f} median={np.median(R):.2f} "
          f"p25={np.percentile(R,25):.2f} p75={np.percentile(R,75):.2f} p90={np.percentile(R,90):.2f} max={R.max():.2f}")
    for thr in (1, 2, 3):
        print(f"  reached >= {thr}R MFE: {(R>=thr).sum()} ({100*(R>=thr).mean():.1f}%)")
    btb = np.array([t["bars_to_breach"] if t["bars_to_breach"] is not None else 10**9 for t in trades])
    print("stop timing (days after entry day):")
    for dd in (1, 2, 3, 5, 10):
        print(f"  stopped within {dd}d: {(btb<=dd).sum()} ({100*(btb<=dd).mean():.1f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["validate","sample","full"], default="validate")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    a = ap.parse_args()
    if a.mode == "validate":
        mode_validate(a.n)
    elif a.mode == "sample":
        mode_run(a.n, a.workers, full=False)
    else:
        mode_run(a.n, a.workers, full=True)


if __name__ == "__main__":
    main()
