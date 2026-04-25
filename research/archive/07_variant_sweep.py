"""Sweep a set of signal-bar-anchored stop rules against all 4 setups.

Each rule is evaluated by race (intraday stop breach vs close-eval exit fire), with the
race window bounded by the earnings cap and the 120-bar hard ceiling.  No entry-bar
info is used.

Variants (long -- short mirrors):
  A0   stop = signal_low
  A_K  stop = signal_low - K * ADR              for K in {0.25, 0.5, 1.0}
  C_K  stop = signal_close - K * ADR            for K in {1.0}
  F_K  stop = min(FW_lows) - K * ADR            for K in {0, 0.25, 0.5}
  FW0  stop = min(FW_lows)                     (K=0, deepest FW low only)
  H0   stop = max(signal_close, max(FW_closes))   (floor at entry-ish for longs)
  S20  stop = min(low of last 20 bars at signal) - 0.25 * ADR     ("recent support")

For DTSS (short) the "low" operations mirror to "high", and the signs flip:
  A_K  stop = signal_high + K * ADR
  etc.

Forward window used = pyramid file's summary? No — FW_max comes from example data
(max of example FW distances).  We compute it per-setup from examples' bar indices
(entry_idx - leftmost_signal_idx where signal bars are consecutive).  If not derivable
from the final_signals list, we default to 3.

Report per setup per variant:
  - example false-negatives (FN): known-win examples the rule calls LOSS
  - matched example pass % (FN / matched)
  - non-example total / win / loss / WR %
  - mean stop-distance-from-signal-close in ADR (sanity)
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3
from collections import Counter, defaultdict
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
sys.path.insert(0, os.path.join(MAIN_ROOT, "scripts"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache  # noqa: E402

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
EXIT_DIR = os.path.join(MAIN_ROOT, "data", "signal_exit_grind")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)


def latest_pyramid(setup):
    fs = glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json"))
    fs = [f for f in fs if "sig0_pk0" not in os.path.basename(f)]
    wt = [(json.load(open(f))["timestamp"], f) for f in fs]
    wt.sort()
    return wt[-1][1] if wt else None


def final_signals(pyr):
    out = []
    for _, tr in pyr["tier_results"].items():
        if tr.get("final_signals"):
            out = tr["final_signals"]
    return out


def load_exit(setup):
    return json.load(open(os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")))["top_conditions"][0]


def load_direction(setup):
    with sqlite3.connect(DB) as c:
        r = c.execute("SELECT direction FROM setups WHERE setup_type=?", (setup,)).fetchone()
    return r[0]


def load_earnings():
    with sqlite3.connect(DB) as c:
        rows = c.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    m = defaultdict(list)
    for tk, ed in rows:
        m[tk].append(str(ed)[:10])
    return {tk: np.array(sorted(set(v))) for tk, v in m.items()}


def align_ohlcv(df, cd):
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        ohlcv_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    else:
        ohlcv_dates = np.array([str(d)[:10] for d in df["date"].values])
    hits = np.where(ohlcv_dates == str(cd[0])[:10])[0]
    if len(hits) == 0:
        return None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    return df_t if len(df_t) == len(cd) else None


def earnings_cap(cache_dates, ern_sorted, sig_date_str):
    if ern_sorted is None or len(ern_sorted) == 0:
        return None
    pos = int(np.searchsorted(ern_sorted, sig_date_str, side="right"))
    if pos >= len(ern_sorted):
        return None
    ern = str(ern_sorted[pos])
    bp = int(np.searchsorted(np.asarray(cache_dates), ern, side="left"))
    return bp - 1 if bp > 0 else None


def adr_at(df_t, i):
    s = max(0, i - 13)
    h = df_t["high"].values[s:i + 1]
    l = df_t["low"].values[s:i + 1]
    return float(np.mean(h - l)) if len(h) else float("nan")


def compute_stop(variant, df_t, signal_idx, direction, fw_size, adr):
    lows = df_t["low"].values
    highs = df_t["high"].values
    closes = df_t["close"].values
    sl = float(lows[signal_idx])
    sh = float(highs[signal_idx])
    sc = float(closes[signal_idx])
    n = len(df_t)
    fw_end = min(signal_idx + fw_size, n - 1)
    fw_slice_lows = lows[signal_idx + 1: fw_end + 1] if fw_end > signal_idx else np.array([sl])
    fw_slice_highs = highs[signal_idx + 1: fw_end + 1] if fw_end > signal_idx else np.array([sh])
    fw_slice_closes = closes[signal_idx + 1: fw_end + 1] if fw_end > signal_idx else np.array([sc])
    s20_start = max(0, signal_idx - 19)
    s20_low = float(np.min(lows[s20_start: signal_idx + 1]))
    s20_high = float(np.max(highs[s20_start: signal_idx + 1]))

    if direction == "long":
        if variant == "A0":  return sl
        if variant == "A_0.25": return sl - 0.25 * adr
        if variant == "A_0.5":  return sl - 0.5  * adr
        if variant == "A_1.0":  return sl - 1.0  * adr
        if variant == "C_1.0":  return sc - 1.0  * adr
        if variant == "FW0":    return float(np.min(fw_slice_lows))
        if variant == "F_0":    return float(np.min(fw_slice_lows))
        if variant == "F_0.25": return float(np.min(fw_slice_lows)) - 0.25 * adr
        if variant == "F_0.5":  return float(np.min(fw_slice_lows)) - 0.5  * adr
        if variant == "H0":     return min(sc, float(np.min(fw_slice_closes)))
        if variant == "S20":    return s20_low - 0.25 * adr
    else:  # short
        if variant == "A0":  return sh
        if variant == "A_0.25": return sh + 0.25 * adr
        if variant == "A_0.5":  return sh + 0.5  * adr
        if variant == "A_1.0":  return sh + 1.0  * adr
        if variant == "C_1.0":  return sc + 1.0  * adr
        if variant == "FW0":    return float(np.max(fw_slice_highs))
        if variant == "F_0":    return float(np.max(fw_slice_highs))
        if variant == "F_0.25": return float(np.max(fw_slice_highs)) + 0.25 * adr
        if variant == "F_0.5":  return float(np.max(fw_slice_highs)) + 0.5  * adr
        if variant == "H0":     return max(sc, float(np.max(fw_slice_closes)))
        if variant == "S20":    return s20_high + 0.25 * adr
    raise ValueError(variant)


def race(df_t, exit_series, signal_idx, direction, exit_dir, thresh, stop_level,
         earnings_bar_cap, race_cap=120):
    n = len(df_t)
    if signal_idx + 1 >= n:
        return "WIN"
    race_start = signal_idx + 1
    caps = [signal_idx + race_cap, n - 1]
    if earnings_bar_cap is not None:
        caps.append(earnings_bar_cap)
    race_end = min(caps)
    if race_start > race_end:
        return "WIN"
    lows = df_t["low"].values[race_start:race_end + 1]
    highs = df_t["high"].values[race_start:race_end + 1]
    if direction == "long":
        stop_hits = np.where(lows < stop_level)[0]
    else:
        stop_hits = np.where(highs > stop_level)[0]
    first_stop = int(stop_hits[0]) if len(stop_hits) else None
    ex = exit_series[race_start:race_end + 1]
    fin = ~np.isnan(ex)
    if exit_dir in (">=", "above"):
        ex_hits = np.where(fin & (ex >= thresh))[0]
    else:
        ex_hits = np.where(fin & (ex <= thresh))[0]
    first_exit = int(ex_hits[0]) if len(ex_hits) else None
    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return "LOSS"
    return "WIN"


def build_clusters(sig_bars, example_signal_set):
    clusters = []
    i = 0
    while i < len(sig_bars):
        j = i + 1
        tk = sig_bars[i][0]
        while j < len(sig_bars):
            if sig_bars[j][0] != tk: break
            if sig_bars[j][1] != sig_bars[j - 1][1] + 1: break
            if (tk, sig_bars[j - 1][1]) in example_signal_set: break
            j += 1
        rightmost = sig_bars[j - 1][1]
        clusters.append({"ticker": tk, "rightmost": rightmost,
                         "is_example": (tk, rightmost) in example_signal_set})
        i = j
    return clusters


def run_setup(setup, universe, expr_cache, earnings_map):
    pyr = json.load(open(latest_pyramid(setup)))
    direction = load_direction(setup)
    exit_cond = load_exit(setup)
    exit_col = expr_cache.expr_index(exit_cond["expression"])
    fw_max = 3  # use the grinder's setting; reading it precisely requires cluster gather

    examples = pyr["example_signals"]
    signals = final_signals(pyr)

    t_cache = {}
    uts = {s["ticker"] for s in signals} | {e["ticker"] for e in examples}
    for tk in uts:
        cd, cdata = expr_cache.get_ticker(tk)
        if cd is None: continue
        df = universe.get(tk)
        if df is None: continue
        df_t = align_ohlcv(df, cd)
        if df_t is None: continue
        cds = [str(d)[:10] for d in cd]
        t_cache[tk] = (df_t, cdata[:, exit_col], cds)

    # example signal set (signal_idx = entry_idx - 1)
    example_signal_set = {}
    for ex in examples:
        tk = ex["ticker"]; ed = ex["entry_date"]
        if tk not in t_cache: continue
        cds = t_cache[tk][2]
        if ed not in cds: continue
        eidx = cds.index(ed)
        if eidx < 1: continue
        example_signal_set[(tk, eidx - 1)] = ex

    sig_bars = []
    for s in signals:
        tk = s["ticker"]; sd = s["date"]
        if tk not in t_cache: continue
        cds = t_cache[tk][2]
        if sd not in cds: continue
        sig_bars.append((tk, cds.index(sd)))
    sig_bars = sorted(set(sig_bars))
    clusters = build_clusters(sig_bars, example_signal_set)

    variants = ["A0", "A_0.25", "A_0.5", "A_1.0", "C_1.0", "FW0", "F_0.25", "F_0.5", "H0", "S20"]
    results_by_variant = {v: {"ex_win": 0, "ex_loss": 0, "nex_win": 0, "nex_loss": 0,
                              "ex_loss_list": []} for v in variants}

    for c in clusters:
        tk = c["ticker"]
        if tk not in t_cache: continue
        df_t, es, cds = t_cache[tk]
        sig_idx = c["rightmost"]
        sig_date = cds[sig_idx]
        adr = adr_at(df_t, sig_idx)
        if not np.isfinite(adr) or adr <= 0: continue
        ern = earnings_cap(cds, earnings_map.get(tk), sig_date)
        for v in variants:
            try:
                stop = compute_stop(v, df_t, sig_idx, direction, fw_max, adr)
            except Exception:
                continue
            outcome = race(df_t, es, sig_idx, direction, exit_cond["direction"],
                           float(exit_cond["threshold"]), stop, ern)
            slot = results_by_variant[v]
            if c["is_example"]:
                if outcome == "WIN":
                    slot["ex_win"] += 1
                else:
                    slot["ex_loss"] += 1
                    slot["ex_loss_list"].append(tk + " " + sig_date)
            else:
                if outcome == "WIN":
                    slot["nex_win"] += 1
                else:
                    slot["nex_loss"] += 1

    rows = []
    for v in variants:
        r = results_by_variant[v]
        ex_total = r["ex_win"] + r["ex_loss"]
        nex_total = r["nex_win"] + r["nex_loss"]
        rows.append({
            "setup": setup, "variant": v,
            "ex_matched": ex_total,
            "ex_FN": r["ex_loss"],
            "ex_pass_pct": round(100 * r["ex_win"] / max(ex_total, 1), 1),
            "nex": nex_total,
            "nex_wr": round(100 * r["nex_win"] / max(nex_total, 1), 1),
            "ex_fn_sample": r["ex_loss_list"][:5],
        })
    return rows


def main():
    print("Load OHLCV + expr cache + earnings...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    expr_cache = ExprSeriesCache()
    earnings_map = load_earnings()
    all_rows = []
    for s in ["dtss", "htf", "bf", "base"]:
        print(f"\n=== {s.upper()} ===", flush=True)
        all_rows.extend(run_setup(s, universe, expr_cache, earnings_map))

    df = pd.DataFrame(all_rows)
    # Pretty print
    print()
    print(df[["setup", "variant", "ex_matched", "ex_FN", "ex_pass_pct", "nex", "nex_wr"]].to_string(index=False))
    df.to_csv(os.path.join(OUT_DIR, "variant_sweep.csv"), index=False)


if __name__ == "__main__":
    main()
