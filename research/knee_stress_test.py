"""
Stress-test the knee-based reversal-profile derivation across tickers.

For each ticker: compute ext50 return-rate curves on BOTH sides (upcrossings
returning below L within 14 bars; downcrossings returning above L within 14 bars).
Apply knee detection (max perpendicular distance from chord on normalized curve)
to identify chop ceiling, reversal onset, and extended boundaries on each side.

Report numbers. Flag degenerate curves where knee detection breaks down.
"""

import os
import io
import urllib.request
import numpy as np
import pandas as pd

TICKERS = [
    # mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA",
    # mature dividend / staples
    "KO", "PG", "JNJ", "WMT", "XOM", "CVX", "VZ", "MCD", "HD",
    # industrials
    "F", "GM", "GE", "BA", "CAT", "DE",
    # financials
    "JPM", "BAC", "GS",
    # healthcare
    "UNH", "LLY", "PFE",
    # commodities / miners
    "FCX", "NEM", "GOLD", "AA", "X",
    # volatile growth
    "PLTR", "SNOW", "SHOP", "CRWD",
    # recent IPOs / SPACs
    "RIVN", "HOOD", "COIN", "RBLX", "SOFI",
    # ETFs
    "SPY", "QQQ", "IWM", "XLE",
]
STAT_START = "2020-01-02"  # EXPR_CACHE_START per Dan's rule
FWD = 14  # ADR14 period = metric's denominator


def fetch(ticker):
    token = os.environ["EODHD_API_TOKEN"]
    url = (
        f"https://eodhd.com/api/eod/{ticker}.US"
        f"?from=2015-01-01&period=d&fmt=csv&api_token={token}"
    )
    with urllib.request.urlopen(url, timeout=60) as r:
        raw = r.read().decode("utf-8")
    df = pd.read_csv(io.StringIO(raw))
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    ratio = df["adjusted_close"] / df["close"]
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * ratio
    return df


def compute_ext(df):
    sma50 = df["close"].rolling(50).mean()
    adr14 = (df["high"] - df["low"]).rolling(14).mean()
    ext = (df["close"] - sma50) / adr14
    ext = ext.dropna()
    return ext[ext.index >= STAT_START]


def return_curve(vals, fwd, side):
    """
    side='up': for each L>0, fraction of upcrossings (prior<L, current>=L) that
              return below L within fwd bars.
    side='down': for each L<0, fraction of downcrossings (prior>L, current<=L)
                 that return above L within fwd bars.
    Returns sorted lists: Ls (ascending absolute magnitude), rates, crosses.
    """
    out = []
    max_abs = int(np.ceil(max(abs(vals.min()), abs(vals.max())))) + 1
    L_range = np.arange(0.5, max_abs + 0.5, 0.5)
    for mag in L_range:
        L = mag if side == "up" else -mag
        crosses = returned = 0
        for t in range(1, len(vals) - fwd):
            if side == "up" and vals[t - 1] < L <= vals[t]:
                crosses += 1
                if (vals[t:t + fwd + 1] < L).any():
                    returned += 1
            elif side == "down" and vals[t - 1] > L >= vals[t]:
                crosses += 1
                if (vals[t:t + fwd + 1] > L).any():
                    returned += 1
        if crosses > 0:
            out.append((L, crosses, returned / crosses))
    return out


def knee_via_perp(xs, ys):
    """
    Knee detection via max perpendicular distance from the chord connecting
    the curve's endpoints. Both curves are normalized to unit square first
    so L-scale and rate-scale don't bias the geometry.

    Returns (idx_pos, idx_neg, max_pos_perp, max_neg_perp).
    For an S-curve, max positive perp and max negative perp are the two
    knees. Caller compares their magnitudes: if the positive perp doesn't
    dominate, the curve is effectively concave (no real upper bend).
    """
    xs = np.array(xs, dtype=float)
    ys = np.array(ys, dtype=float)
    if len(xs) < 3:
        return None, None, 0.0, 0.0
    xn = (xs - xs.min()) / (xs.max() - xs.min()) if xs.max() > xs.min() else xs * 0
    yn = (ys - ys.min()) / (ys.max() - ys.min()) if ys.max() > ys.min() else ys * 0
    p0 = np.array([xn[0], yn[0]])
    p1 = np.array([xn[-1], yn[-1]])
    lv = p1 - p0
    ll = np.linalg.norm(lv)
    if ll == 0:
        return None, None, 0.0, 0.0
    perp = np.array([
        (lv[0] * (yn[i] - p0[1]) - lv[1] * (xn[i] - p0[0])) / ll
        for i in range(len(xn))
    ])
    return int(np.argmax(perp)), int(np.argmin(perp)), float(perp.max()), float(perp.min())


def derive_levels(curve, side):
    """
    Upside: one metric.
      reversal_level = upper knee of the rising portion of the curve.
    Downside: two metrics.
      bounce_level = upper knee of the rising portion (before the rate peak).
      capitulation_level = knee of the declining portion (after the rate peak)
                           where rate drops — signifies regime flip to trend.
                           None if the downside curve is purely monotonic
                           (no capitulation regime observed in this window).
    Returns (dict of level→L or None, meta dict).
    """
    out = {}
    meta = dict(n_levels=len(curve), degenerate=False)
    if len(curve) < 3:
        return out, dict(meta, degenerate=True, reason="too short")
    Ls = np.array([abs(x[0]) for x in curve])
    crs = np.array([x[1] for x in curve])
    rates = np.array([x[2] for x in curve])

    meta["total_crossings"] = int(crs.sum())
    meta["peak_crossings"] = int(crs.max())
    meta["rate_min"] = float(rates.min())
    meta["rate_max"] = float(rates.max())

    argmax_idx = int(np.argmax(rates))
    meta["argmax_L"] = float(Ls[argmax_idx])
    meta["argmax_rate"] = float(rates[argmax_idx])

    if argmax_idx < 2:
        return out, dict(meta, degenerate=True, reason="rate peaks at start")

    # Bounce / reversal: knee of the rising portion up to global argmax.
    # For clean S-curves with a convex bend toward saturation, knee via
    # max positive perp distance from the chord captures it.
    # For concave curves (below chord throughout), max positive perp lands
    # at an endpoint — no real knee. Fall back to argmax (first L where rate
    # hits its observed max) — the data-derived saturation level.
    Ls_rising = Ls[: argmax_idx + 1]
    rates_rising = rates[: argmax_idx + 1]
    k_pos, k_neg, max_pos, max_neg = knee_via_perp(Ls_rising, rates_rising)
    # A legitimate upper knee has rate on the rising side of the curve:
    # rate_at_knee must exceed the midpoint of the observed rate range.
    # If it doesn't, the "knee" is just a noise bump in the chop plateau;
    # fall back to argmax (first L where rate hits its observed max).
    if k_pos is not None:
        rate_range_mid = (rates_rising.min() + rates_rising.max()) / 2
        if rates_rising[k_pos] < rate_range_mid:
            k_pos = None
    if k_pos is None or k_pos == 0 or k_pos == len(Ls_rising) - 1:
        k_pos = argmax_idx
        meta["fallback"] = "argmax (no valid upper knee)"

    if side == "up":
        out["reversal"] = float(Ls[k_pos])
        meta["reversal_rate"] = float(rates_rising[k_pos])
    else:
        out["bounce"] = float(Ls[k_pos])
        meta["bounce_rate"] = float(rates_rising[k_pos])
        # Capitulation: largest rate dip below the running max of the rate
        # curve. A two-regime curve climbs to some peak, drops significantly
        # (capitulation = trend continuation regime), then may recover.
        # Compute: for each L, running_max_rate up to that L. capitulation_L
        # = argmax of (running_max - rate) — the point with the biggest dip
        # below its local ceiling. Only emit if the dip is larger than any
        # rate difference in the rising portion (i.e., a real regime flip,
        # not just climb noise).
        running_max = np.maximum.accumulate(rates)
        dip = running_max - rates
        # Max allowed noise-dip = worst dip in the rising portion up to k_pos
        rising_noise = float(np.max(running_max[:k_pos + 1] - rates[:k_pos + 1])) if k_pos > 0 else 0.0
        post_peak_dip = dip[k_pos + 1:]
        if len(post_peak_dip) > 0 and post_peak_dip.max() > rising_noise:
            cap_local_idx = int(np.argmax(post_peak_dip))
            cap_idx = k_pos + 1 + cap_local_idx
            out["capitulation"] = float(Ls[cap_idx])
            meta["capitulation_rate"] = float(rates[cap_idx])

    return out, meta


def analyze_ticker(ticker):
    try:
        df = fetch(ticker)
    except Exception as e:
        return dict(ticker=ticker, error=str(e)[:40])
    ext = compute_ext(df)
    if len(ext) < 250:
        return dict(ticker=ticker, error=f"only {len(ext)} bars")
    vals = ext.values
    out = dict(ticker=ticker, bars=len(ext),
               ext_lo=float(vals.min()), ext_hi=float(vals.max()),
               sigma=float(np.std(vals)))
    for side in ("up", "down"):
        curve = return_curve(vals, FWD, side)
        if not curve:
            out[f"{side}_levels"] = {}
            continue
        levels, meta = derive_levels(curve, side)
        out[f"{side}_levels"] = levels
        out[f"{side}_meta"] = meta
        out[f"{side}_crossings"] = meta.get("total_crossings", 0)
    return out


if __name__ == "__main__":
    print(f"single-metric reversal_level stress test")
    print(f"stat window: {STAT_START}+   forward window: {FWD} bars (ADR14)")
    print(f"method: knee via perpendicular distance on return-rate curve")
    print()
    print(f"{'ticker':<7} {'bars':>5} {'ext_lo':>7} {'ext_hi':>7}   "
          f"{'up rev':>7} {'@rate':>6} {'up_N':>5}   "
          f"{'dn bnc':>7} {'@rate':>6}   {'dn cap':>7} {'@rate':>6} {'dn_N':>5}")
    print("-" * 120)
    results = []
    for t in TICKERS:
        r = analyze_ticker(t)
        results.append(r)
        if "error" in r:
            print(f"{t:<7} ERROR: {r['error']}")
            continue
        up_lvls = r.get("up_levels", {})
        down_lvls = r.get("down_levels", {})
        up_meta = r.get("up_meta", {})
        down_meta = r.get("down_meta", {})

        up_rev = f"+{up_lvls['reversal']:.1f}" if "reversal" in up_lvls else "n/a"
        up_rr  = f"{100*up_meta.get('reversal_rate',0):.0f}%" if "reversal" in up_lvls else ""
        dn_bnc = f"-{down_lvls['bounce']:.1f}" if "bounce" in down_lvls else "n/a"
        dn_br  = f"{100*down_meta.get('bounce_rate',0):.0f}%" if "bounce" in down_lvls else ""
        dn_cap = f"-{down_lvls['capitulation']:.1f}" if "capitulation" in down_lvls else "n/a"
        dn_cr  = f"{100*down_meta.get('capitulation_rate',0):.0f}%" if "capitulation" in down_lvls else ""

        print(f"{t:<7} {r['bars']:>5} {r['ext_lo']:>+7.2f} {r['ext_hi']:>+7.2f}   "
              f"{up_rev:>7} {up_rr:>6} {r.get('up_crossings',0):>5}   "
              f"{dn_bnc:>7} {dn_br:>6}   {dn_cap:>7} {dn_cr:>6} {r.get('down_crossings',0):>5}")

    up_list = [r["up_levels"]["reversal"] for r in results if "up_levels" in r and "reversal" in r["up_levels"]]
    dn_bnc_list = [r["dn_levels"]["bounce"] for r in results if "dn_levels" in r and "bounce" in r["dn_levels"]]
    dn_cap_list = [r["dn_levels"]["capitulation"] for r in results if "dn_levels" in r and "capitulation" in r["dn_levels"]]
    print()
    print(f"summary:")
    if up_list:
        print(f"  upside reversal_level   n={len(up_list):<3d}  median {np.median(up_list):.2f}  range [{min(up_list):.2f}, {max(up_list):.2f}]")
    if dn_bnc_list:
        print(f"  downside bounce_level   n={len(dn_bnc_list):<3d}  median {np.median(dn_bnc_list):.2f}  range [{min(dn_bnc_list):.2f}, {max(dn_bnc_list):.2f}]")
    if dn_cap_list:
        print(f"  downside capitulation   n={len(dn_cap_list):<3d}  median {np.median(dn_cap_list):.2f}  range [{min(dn_cap_list):.2f}, {max(dn_cap_list):.2f}]")
