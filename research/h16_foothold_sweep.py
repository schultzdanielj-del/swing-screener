"""H16: foothold-cap sweep.

Rule per Dan (2026-04-23):
- Support MA = longest-period MA in {EMA3,8,21, SMA50,100,200} with
  |sig_low - MA| / ADR <= FOOTHOLD_CAP.
- Lookback window = that MA's period.
- Resistance AVWAP = argmax AVWAP in [sig - period, sig - 1].
- Corridor width = (resistance AVWAP - support MA) / ADR.

For a range of foothold caps, report:
- how many examples produce valid (corridor > 0) results
- per-setup corridor distribution (min, p25, p50, p75, p90, max)
- per-setup support-MA selection distribution
- DRN and QUBT detailed rows at each cap
"""
import pickle
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

CACHE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"
DB = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/data/scanperfect.db"
MA_SET = [("EMA", 3), ("EMA", 8), ("EMA", 21), ("SMA", 50), ("SMA", 100), ("SMA", 200)]
CAPS = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50]

with open(CACHE, "rb") as f:
    universe = pickle.load(f)
with sqlite3.connect(DB) as conn:
    rows = conn.execute("SELECT setup_type, ticker, entry_date FROM examples "
                        "WHERE setup_type IN ('htf','bf','base') "
                        "ORDER BY setup_type, ticker, entry_date").fetchall()


def load(ticker):
    df = universe[ticker]
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        df = df.copy(); df["date"] = pd.to_datetime(df["date"])
    d = df["date"].dt.strftime("%Y-%m-%d").values
    return (d, df["high"].values.astype(float), df["low"].values.astype(float),
            df["close"].values.astype(float), df["volume"].values.astype(float))


def adr14(h, l, i): return float(np.mean(h[i-13:i+1] - l[i-13:i+1])) if i >= 14 else float("nan")
def sma(a, n, i): return float(np.mean(a[i-n+1:i+1])) if i >= n-1 else float("nan")
def ema(a, n, i):
    if i < n-1: return float("nan")
    alpha = 2.0/(n+1); e = a[0]
    for k in range(1, i+1): e = alpha*a[k] + (1-alpha)*e
    return float(e)


def all_ma_footholds(c, l, i, adr):
    """Return list of (period, kind, val, foothold_adr, signed_close_dist_adr)."""
    out = []
    sl = l[i]; sc = c[i]
    for kind, n in MA_SET:
        if i < n-1: continue
        v = sma(c, n, i) if kind=="SMA" else ema(c, n, i)
        if not np.isfinite(v): continue
        out.append((n, kind, v, abs(sl - v)/adr, (sc - v)/adr))
    return out


def pick_support(mas, cap):
    qual = [m for m in mas if m[3] <= cap]
    if not qual: return None
    qual.sort(key=lambda m: -m[0])  # longest period first
    return qual[0]  # (period, kind, val, foothold, signed)


def argmax_avwap(h, l, c, v, i, n_win):
    tp = (h + l + c) / 3.0
    A_start = max(0, i - n_win); A_end = i
    if A_start >= A_end: return None
    ctpv = np.concatenate([[0.0], np.cumsum(tp*v)])
    cv = np.concatenate([[0.0], np.cumsum(v)])
    Ar = np.arange(A_start, A_end)
    tpv = ctpv[i+1] - ctpv[Ar]; vv = cv[i+1] - cv[Ar]
    with np.errstate(invalid="ignore", divide="ignore"):
        av = np.where(vv > 0, tpv/vv, -np.inf)
    bi = int(np.argmax(av))
    return int(Ar[bi]), float(av[bi])


# Per-example precompute
examples = []
for setup, ticker, entry_date in rows:
    if ticker not in universe: continue
    d, h, l, c, v = load(ticker)
    hits = np.where(d == entry_date)[0]
    if len(hits) == 0: continue
    i = int(hits[0]) - 1
    if i < 14: continue
    adr = adr14(h, l, i)
    if not np.isfinite(adr) or adr <= 0: continue
    examples.append({
        "setup": setup, "ticker": ticker, "entry": entry_date,
        "sig_idx": i, "sig_close": c[i], "sig_low": l[i], "adr": adr,
        "dates": d, "h": h, "l": l, "c": c, "v": v,
        "mas": all_ma_footholds(c, l, i, adr),
    })

print(f"Examples: {len(examples)}")
print()


def run_cap(cap):
    results = []
    for ex in examples:
        sup = pick_support(ex["mas"], cap)
        if sup is None:
            results.append({**ex, "cap": cap, "support": None, "reason": "no_MA_within_cap"})
            continue
        period, kind, val, foothold, signed = sup
        r = argmax_avwap(ex["h"], ex["l"], ex["c"], ex["v"], ex["sig_idx"], period)
        if r is None:
            results.append({**ex, "cap": cap, "support": sup, "reason": "no_avwap"})
            continue
        A, avwap = r
        corridor = (avwap - val) / ex["adr"]
        resist_above_sig = avwap > ex["sig_close"]
        results.append({
            "setup": ex["setup"], "ticker": ex["ticker"], "entry": ex["entry"],
            "cap": cap, "support_kind": kind, "support_n": period, "support_val": val,
            "foothold": foothold, "support_dist": signed,
            "anchor_idx": A, "anchor_date": ex["dates"][A],
            "avwap": avwap, "resist_dist": (avwap - ex["sig_close"])/ex["adr"],
            "corridor": corridor, "resist_above_sig": resist_above_sig,
            "sig_close": ex["sig_close"], "adr": ex["adr"],
        })
    return results


for cap in CAPS:
    print(f"{'='*70}")
    print(f"FOOTHOLD CAP = {cap:.2f} ADR")
    print(f"{'='*70}")
    res = run_cap(cap)
    rejected = [r for r in res if "reason" in r]
    kept = [r for r in res if "reason" not in r]
    kept_valid = [r for r in kept if r["resist_above_sig"]]
    print(f"  rejected (no MA within cap): {len(rejected)}")
    print(f"  kept: {len(kept)}")
    print(f"  kept with resistance > sig_close: {len(kept_valid)}")
    print()
    if len(kept) == 0:
        continue
    for setup in ("htf", "bf", "base"):
        sub = [r for r in kept if r["setup"] == setup]
        if not sub: continue
        w = np.array([r["corridor"] for r in sub])
        valid_count = sum(1 for r in sub if r["resist_above_sig"])
        print(f"  {setup:<5} n={len(sub):>3} (valid res>sig: {valid_count})  "
              f"min={np.min(w):+.2f}  p50={np.percentile(w,50):.2f}  "
              f"p90={np.percentile(w,90):.2f}  max={np.max(w):.2f}")
        # MA distribution
        mas = Counter(f"{r['support_kind']}{r['support_n']}" for r in sub)
        print(f"        MA pick: " + "  ".join(f"{ma}:{cnt}" for ma, cnt in mas.most_common()))
    print()
    # Show DRN and QUBT
    for tkr in ("DRN", "QUBT"):
        for r in kept:
            if r["ticker"] != tkr: continue
            print(f"  {tkr} {r['entry']} ({r['setup']}): "
                  f"support={r['support_kind']}{r['support_n']} "
                  f"foothold={r['foothold']:.2f}  "
                  f"anchor={r['anchor_date']} avwap={r['avwap']:.3f} "
                  f"resist_dist={r['resist_dist']:+.2f} ADR  "
                  f"corridor={r['corridor']:+.2f} ADR")
        for r in rejected:
            if r["ticker"] != tkr: continue
            print(f"  {tkr} {r['entry']} ({r['setup']}): REJECTED ({r['reason']})")
    print()

# Foothold distribution summary: what's the tightest foothold that still admits
# each example's "best" (longest-period-valid) support?
print(f"{'='*70}")
print("FOOTHOLD DISTRIBUTION OF LONGEST-MA-WITH-ABOVE-SIG-RESISTANCE PER EXAMPLE")
print(f"{'='*70}")
print("(for each example, find longest-period MA whose argmax AVWAP > sig_close;")
print(" report its foothold value — tells us the minimum cap that admits it)")
print()
per_setup_footholds = {"htf": [], "bf": [], "base": []}
unresolved = []
for ex in examples:
    cand = []
    for period, kind, val, fh, signed in ex["mas"]:
        r = argmax_avwap(ex["h"], ex["l"], ex["c"], ex["v"], ex["sig_idx"], period)
        if r is None: continue
        A, avw = r
        if avw > ex["sig_close"]:
            cand.append((period, kind, val, fh, avw))
    if not cand:
        unresolved.append(ex)
        continue
    cand.sort(key=lambda x: -x[0])
    period, kind, val, fh, avw = cand[0]
    per_setup_footholds[ex["setup"]].append((ex["ticker"], ex["entry"], f"{kind}{period}", fh, avw))

for setup in ("htf", "bf", "base"):
    vals = [f for _,_,_, f, _ in per_setup_footholds[setup]]
    if not vals: continue
    arr = np.array(vals)
    print(f"  {setup:<5} n={len(vals)}  min={arr.min():.2f}  p50={np.percentile(arr,50):.2f}  "
          f"p90={np.percentile(arr,90):.2f}  max={arr.max():.2f}")
    top5 = sorted(per_setup_footholds[setup], key=lambda x: -x[3])[:5]
    for tk, en, ma, fh, av in top5:
        print(f"    {tk:<6} {en}  {ma:<7} foothold={fh:.2f}  avwap={av:.3f}")
print()
print(f"Examples with no valid (above-sig) resistance from any MA: {len(unresolved)}")
for ex in unresolved:
    print(f"  {ex['ticker']} {ex['entry']} ({ex['setup']})")
