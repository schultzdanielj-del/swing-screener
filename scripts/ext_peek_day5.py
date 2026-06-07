"""Day-5 outcome for the confirmation-entry variant on tradable firings.

Entry = first later bar that breaks the trigger bar's high (low not breached
first). Stop / 1R = entry - trigger-bar low. Question: 5 bars after entry,
what % of valid entries are >= 1R above entry?
Read-only; prints stats, writes a per-entry CSV with the day-5 fields.
"""
import os, sys, pickle
import numpy as np
import pandas as pd

ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
CACHE = os.path.join(ROOT, "local_runner", "cache")
TRADABLE_CSV = os.path.join(CACHE, "ext_peek_backtest_20260603_094408_tradable.csv")
HORIZON = 5

def main():
    firings = pd.read_csv(TRADABLE_CSV)
    print(f"tradable firings: {len(firings)}")
    print("cache:", CACHE)
    with open(os.path.join(CACHE, "universe_ohlcv_daily.pkl"), "rb") as f:
        U = pickle.load(f)
    print("universe tickers:", len(U))
    assert len(U) > 11200, "ticker count too low -- STOP"

    rows = []
    n_cancel = 0
    for tk, sub in firings.groupby("ticker", sort=True):
        o = U.get(tk)
        if o is None:
            continue
        dts = o["date"].astype(str).str[:10].values
        op = o["open"].values.astype(np.float64)
        h = o["high"].values.astype(np.float64)
        l = o["low"].values.astype(np.float64)
        c = o["close"].values.astype(np.float64)
        didx = {dd: i for i, dd in enumerate(dts)}
        n = len(h)
        for dt in sub["date"].values:
            t = didx.get(dt)
            if t is None:
                continue
            trig_hi, trig_lo = h[t], l[t]
            j = None; cancelled = False
            for jj in range(t+1, n):
                if l[jj] < trig_lo:
                    cancelled = True; break
                if h[jj] > trig_hi:
                    j = jj; break
            if cancelled or j is None:
                if cancelled: n_cancel += 1
                continue
            entry = op[j] if op[j] > trig_hi else trig_hi
            risk = entry - trig_lo
            if risk <= 0:
                continue
            if j + HORIZON > n - 1:
                continue  # not enough bars after entry to measure day-5
            fwd_lo = l[j+1:j+HORIZON+1]
            fwd_hi = h[j+1:j+HORIZON+1]
            stopped_by5 = bool((fwd_lo < trig_lo).any())
            day5_close = c[j+HORIZON]
            r_day5 = (day5_close - entry) / risk
            touched1R = bool(fwd_hi.max() >= entry + risk)
            rows.append({
                "ticker": tk, "trigger_date": dt, "entry_date": dts[j],
                "entry_delay": j - t, "entry_price": round(entry,4),
                "risk": round(risk,4), "r_at_day5": round(r_day5,3),
                "over1R_day5": r_day5 >= 1.0,
                "stopped_by5": stopped_by5,
                "over1R_day5_alive": (r_day5 >= 1.0) and (not stopped_by5),
                "touched1R_by5": touched1R,
            })

    out = pd.DataFrame(rows)
    n = len(out)
    print(f"\nvalid filled entries with >= {HORIZON} bars after entry: {n}  (cancelled low-first: {n_cancel})")
    if n:
        print(f"\n--- {HORIZON} days after entry (1R = entry - trigger low) ---")
        print(f"close >= 1R above entry (ignoring stop):     {out.over1R_day5.sum()} ({100*out.over1R_day5.mean():.1f}%)")
        print(f"  ... and never tagged trigger low en route: {out.over1R_day5_alive.sum()} ({100*out.over1R_day5_alive.mean():.1f}%)")
        print(f"touched >= 1R at any point within {HORIZON} days:   {out.touched1R_by5.sum()} ({100*out.touched1R_by5.mean():.1f}%)")
        print(f"tagged trigger-low stop within {HORIZON} days:      {out.stopped_by5.sum()} ({100*out.stopped_by5.mean():.1f}%)")
        print(f"median R at day {HORIZON}: {out.r_at_day5.median():.2f}")
        nd = out[out.entry_delay == 1]
        print(f"\n--- next-day entries only (delay==1): {len(nd)} of {n} ---")
        if len(nd):
            print(f"close >= 1R at day {HORIZON} (ignoring stop): {100*nd.over1R_day5.mean():.1f}%   "
                  f"alive & >=1R: {100*nd.over1R_day5_alive.mean():.1f}%   median R: {nd.r_at_day5.median():.2f}")
    out_path = os.path.join(CACHE, "ext_peek_tradable_confirm_day5.csv")
    out.to_csv(out_path, index=False)
    print(f"\nwrote {out_path}")

if __name__ == "__main__":
    main()
