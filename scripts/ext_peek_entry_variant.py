"""Extension Peek -- confirmation-entry variant on the tradable firings.

Instead of entering at the trendline-breaking bar's close, wait for the first
later bar that breaks ABOVE that bar's high and enter there (fill = trigger
high, or the open if it gaps above). Cancel the entry if the trigger bar's low
is breached first. Stop = trigger bar low. MFE before stop, 60 bars from entry.
Read-only; writes one CSV.
"""
import os, sys, pickle
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
CACHE = os.path.join(ROOT, "local_runner", "cache")
WINDOW = 60
TRADABLE_CSV = os.path.join(CACHE, "ext_peek_backtest_20260603_094408_tradable.csv")

def main():
    firings = pd.read_csv(TRADABLE_CSV)
    print(f"tradable firings in: {len(firings)}")
    print("cache:", CACHE)
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        U = pickle.load(f)
    print("universe tickers:", len(U))
    assert len(U) > 11200, "ticker count too low -- STOP"

    rows = []
    n_cancel = n_unresolved = 0
    for tk, sub in firings.groupby("ticker", sort=True):
        o = U.get(tk)
        if o is None:
            n_unresolved += len(sub); continue
        dts = o["date"].astype(str).str[:10].values
        op = o["open"].values.astype(np.float64)
        h = o["high"].values.astype(np.float64)
        l = o["low"].values.astype(np.float64)
        didx = {dd: i for i, dd in enumerate(dts)}
        n = len(h)
        for dt in sub["date"].values:
            t = didx.get(dt)
            if t is None:
                n_unresolved += 1; continue
            trig_hi, trig_lo = h[t], l[t]
            j = None; cancelled = False
            for jj in range(t+1, n):
                if l[jj] < trig_lo:        # low breached before high break -> cancel
                    cancelled = True; break
                if h[jj] > trig_hi:        # high break -> enter
                    j = jj; break
            if cancelled:
                n_cancel += 1; continue
            if j is None:                  # ran out of data, never resolved
                n_unresolved += 1; continue

            entry = op[j] if op[j] > trig_hi else trig_hi
            risk = entry - trig_lo
            if risk <= 0:
                n_unresolved += 1; continue
            end = min(j + WINDOW, n - 1)
            best_hi = h[j]                 # entry bar high counts (filled intrabar)
            stopped = False; breach = None
            for k in range(j+1, end+1):
                if l[k] < trig_lo:
                    stopped = True; breach = k; break
                if h[k] > best_hi:
                    best_hi = h[k]
            mfe = max(0.0, best_hi - entry)
            rows.append({
                "ticker": tk, "trigger_date": dt, "entry_date": dts[j],
                "entry_delay": j - t, "entry_price": round(entry, 4),
                "trig_low": round(trig_lo, 4), "risk": round(risk, 4),
                "mfe_abs": round(mfe, 4), "mfe_R": round(mfe / risk, 3),
                "mfe_pct": round(mfe / entry * 100, 3),
                "stopped": bool(stopped),
                "bars_to_breach": (breach - j) if breach is not None else None,
                "window_trunc": bool(j + WINDOW > n - 1),
            })

    out = pd.DataFrame(rows)
    n_fill = len(out)
    total = len(firings)
    print(f"\n=== CONFIRMATION ENTRY (high-break, low-not-breached) ===")
    print(f"of {total} tradable firings:  filled={n_fill} ({100*n_fill/total:.1f}%)  "
          f"cancelled(low first)={n_cancel} ({100*n_cancel/total:.1f}%)  unresolved={n_unresolved}")
    if n_fill:
        R = out.mfe_R.values; st = out.stopped.values.astype(bool)
        btb = out.bars_to_breach.fillna(10**9).values
        dly = out.entry_delay.values
        print(f"entry delay (bars after trigger): median={np.median(dly):.0f} mean={dly.mean():.1f} max={dly.max()}")
        print(f"stopped(loss): {st.sum()} ({100*st.mean():.1f}%) | survivors: {(~st).sum()} ({100*(~st).mean():.1f}%)")
        print("stop timing (from entry): " + "  ".join(f"<= {dd}d {100*(btb<=dd).mean():.0f}%" for dd in (1,3,5,10,20)))
        print(f"MFE R: median={np.median(R):.2f} p75={np.percentile(R,75):.2f} p90={np.percentile(R,90):.2f} mean={R.mean():.2f} max={R.max():.0f}")
        for thr in (1,2,3,5):
            print(f"  >= {thr}R: {(R>=thr).sum()} ({100*(R>=thr).mean():.1f}%)")
    out_path = os.path.join(CACHE, "ext_peek_backtest_20260603_094408_tradable_confirm_entry.csv")
    out.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
