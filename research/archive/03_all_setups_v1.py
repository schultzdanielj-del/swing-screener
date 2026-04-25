"""Scan + cluster + V1-classify each active setup.

Per setup (DTSS, HTF, BF, BASE):
  1. Load signal conditions from latest pyramid_{setup}_mp_sig*.json (skip sig0_pk0).
  2. Load exit condition from signal_exit_{setup}.json.
  3. Load direction from SQLite setups table.
  4. Load examples (ticker + entry_date) from SQLite examples table.
  5. Scan universe with signal conditions (cache-relative), producing signal-bar hits.
  6. Cluster consecutive signal bars; split clusters at example signal bars so the
     rightmost of an example's own cluster IS the example signal bar.
  7. Apply V1 classification:
        signal_idx = cluster.rightmost.bar_idx
        entry_idx  = signal_idx + 1
        stop       = low[entry_idx]   (long)   /  high[entry_idx]  (short)
        race_start = entry_idx + 1
        race_end   = min(signal_idx + 120, end_of_data)
        LOSS if any bar in race_range has low < stop (long) / high > stop (short)
             strictly BEFORE exit condition fires on a close.  Same-bar tie -> stop wins.
        WIN  if exit fires first.
        WIN  if neither fires by race_end (timeout).
     Example-override OFF for this accuracy test: we want to see natural pass rate.

Accuracy metric we report:
  - n_examples_seen  (examples that matched a cluster in the scan)
  - n_examples_pass_v1  (examples that classified WIN under V1 without override)
  - n_example_bugs  (examples where entry-1 isn't in the signal scan — grinder bugs)
  - Cluster counts and non-example win rate (for context only, NOT a target).

Output: research/out/v1_allsetups.csv + stdout summary.
"""
from __future__ import annotations

import os, sys, glob, json, pickle, time, sqlite3
from collections import defaultdict, Counter
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
sys.path.insert(0, os.path.join(MAIN_ROOT, "scripts"))
os.chdir(MAIN_ROOT)

from expr_cache_builder import ExprSeriesCache  # noqa: E402

RESEARCH_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(RESEARCH_OUT, exist_ok=True)

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
EXIT_DIR = os.path.join(MAIN_ROOT, "data", "signal_exit_grind")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")


def latest_pyramid_file(setup):
    files = sorted(glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json")))
    files = [f for f in files if "sig0_pk0" not in os.path.basename(f)]
    return files[-1] if files else None


def load_signal_conditions(setup):
    p = latest_pyramid_file(setup)
    if p is None:
        return None, None
    with open(p) as f:
        d = json.load(f)
    # conditions stored as list under "conditions" or "all_conditions"
    conds = d.get("conditions") or d.get("all_conditions") or d.get("final_conditions")
    return conds, os.path.basename(p)


def load_exit_cond(setup):
    p = os.path.join(EXIT_DIR, f"signal_exit_{setup}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        d = json.load(f)
    tops = d.get("top_conditions")
    if not tops:
        return None
    return tops[0]  # best-ranked exit condition


def load_direction(setup):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT direction FROM setups WHERE setup_type=?", (setup,)).fetchone()
    return row[0] if row else None


def load_examples(setup):
    with sqlite3.connect(DB) as c:
        rows = c.execute(
            "SELECT ticker, entry_date FROM examples WHERE setup_type=?",
            (setup,),
        ).fetchall()
    return [(t, d) for t, d in rows]


def compute_tradable_masks(universe_cache, expr_cache):
    """Per-bar tradable mask (price>=$1, dvol_20d>=4M, adrp20>=1.8%)."""
    masks = {}
    for tk, df in universe_cache.items():
        if df is None or len(df) < 20:
            continue
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        v = df["volume"].values
        # dvol_20d already a column?
        if "dvol_20d" in df.columns:
            dvol = df["dvol_20d"].values
        else:
            dvol = pd.Series(c * v).rolling(20, min_periods=1).mean().values
        # 20-bar ADRP TC2000 style: (mean(h/l) - 1)*100
        with np.errstate(divide="ignore", invalid="ignore"):
            hl = np.where(l > 0, h / l, 1.0)
        adrp20 = pd.Series(hl).rolling(20, min_periods=1).mean().values
        adrp20 = (adrp20 - 1.0) * 100.0
        m = (c >= 1.0) & (dvol >= 4_000_000) & (adrp20 >= 1.8)
        masks[tk] = m
    return masks


def align_ohlcv(df, cd):
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        ohlcv_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    else:
        ohlcv_dates = np.array([str(d)[:10] for d in df["date"].values])
    expr_first = str(cd[0])[:10]
    hits = np.where(ohlcv_dates == expr_first)[0]
    if len(hits) == 0:
        return None, None
    off = int(hits[0])
    end = min(off + len(cd), len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != len(cd):
        return None, None
    return df_t, off


def scan_signals(setup, conditions, universe_cache, expr_cache, example_tickers):
    """Returns dict ticker -> list of signal bar indices (cache-relative)."""
    cond_cols = [expr_cache.expr_index(c["name"]) for c in conditions]
    if any(col is None for col in cond_cols):
        missing = [c["name"] for c, col in zip(conditions, cond_cols) if col is None]
        print(f"    WARNING: {len(missing)} conditions missing from expr cache", flush=True)

    print(f"    Building tradable masks...", flush=True)
    trad_masks = compute_tradable_masks(universe_cache, expr_cache)

    out = {}
    t0 = time.time()
    n = 0
    for tk, df in universe_cache.items():
        if df is None or len(df) < 100:
            continue
        cd, cdata = expr_cache.get_ticker(tk)
        if cd is None or len(cd) < 100:
            continue
        n_bars = len(cd)
        pass_mask = np.ones(n_bars, dtype=bool)
        # Warmup 50 bars (exempt for example tickers)
        if tk not in example_tickers:
            pass_mask[:50] = False
            tm = trad_masks.get(tk)
            if tm is None:
                continue
            df_t, _off = align_ohlcv(df, cd)
            if df_t is None:
                continue
            # Recompute tradable mask aligned to expr cache dates
            tm_aligned = tm[_off:_off + n_bars] if tm is not None else None
            if tm_aligned is None or len(tm_aligned) != n_bars:
                continue
            pass_mask &= tm_aligned
        if not pass_mask.any():
            continue
        for ci, col in enumerate(cond_cols):
            if col is None:
                pass_mask[:] = False
                break
            series = cdata[:, col].astype(np.float64)
            in_range = (series >= conditions[ci]["low"]) & (series <= conditions[ci]["high"])
            in_range[np.isnan(series)] = False
            pass_mask &= in_range
            if not pass_mask.any():
                break
        idxs = np.where(pass_mask)[0].tolist()
        if idxs:
            out[tk] = idxs
        n += 1
        if n % 1000 == 0:
            print(f"    scanned {n} tickers ({time.time()-t0:.0f}s) -> {sum(len(v) for v in out.values())} signals so far", flush=True)
    print(f"    Scan done: {n} tickers, {sum(len(v) for v in out.values())} signals ({time.time()-t0:.0f}s)", flush=True)
    return out


def build_clusters(raw_signals_by_ticker, example_signal_set):
    """raw_signals_by_ticker: {ticker: [bar_idx sorted ascending]}.
    example_signal_set: {(ticker, bar_idx): (entry_date, entry_idx)}.
    Returns list of clusters (one per dedup), each with rightmost bar_idx and leftward bars."""
    clusters = []
    cid = 0
    for tk, idxs in raw_signals_by_ticker.items():
        if not idxs:
            continue
        idxs = sorted(idxs)
        i = 0
        while i < len(idxs):
            j = i + 1
            while j < len(idxs) and idxs[j] == idxs[j - 1] + 1:
                # End cluster if previous bar is an example signal bar
                if (tk, idxs[j - 1]) in example_signal_set:
                    break
                j += 1
            rightmost = idxs[j - 1]
            leftward = idxs[i:j - 1]
            clusters.append({
                "cluster_id": cid,
                "ticker": tk,
                "rightmost_idx": rightmost,
                "leftward_idxs": leftward,
                "size": j - i,
            })
            cid += 1
            i = j
    return clusters


def classify_v1(cluster, df_t, exit_series, exit_dir, exit_thresh, direction, race_cap=120):
    """V1: entry = signal+1. stop = entry_bar extreme. race from signal+2.
    Returns (cls, reason, exit_bar_rel, stop_bar_rel).
    """
    sig = cluster["rightmost_idx"]
    n = len(df_t)
    if sig + 1 >= n:
        return ("WIN", "no_entry_bar", None, None)
    entry = sig + 1
    if direction == "long":
        stop_level = float(df_t["low"].values[entry])
    else:
        stop_level = float(df_t["high"].values[entry])
    race_start = entry + 1
    race_end = min(sig + race_cap, n - 1)
    if race_start > race_end:
        return ("WIN", "no_race_window", None, None)
    lows = df_t["low"].values[race_start:race_end + 1]
    highs = df_t["high"].values[race_start:race_end + 1]
    if direction == "long":
        stop_hits = np.where(lows < stop_level)[0]
    else:
        stop_hits = np.where(highs > stop_level)[0]
    first_stop = int(stop_hits[0]) if len(stop_hits) else None

    ex_slice = exit_series[race_start:race_end + 1]
    finite = ~np.isnan(ex_slice)
    if exit_dir in (">=", "above"):
        ex_hits = np.where(finite & (ex_slice >= exit_thresh))[0]
    else:
        ex_hits = np.where(finite & (ex_slice <= exit_thresh))[0]
    first_exit = int(ex_hits[0]) if len(ex_hits) else None

    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return ("LOSS", "stop_hit", None, first_stop + (race_start - sig))
    if first_exit is not None:
        return ("WIN", "exit_fired", first_exit + (race_start - sig), None)
    return ("WIN", "timeout", None, None)


def run_setup(setup, universe_cache, expr_cache):
    print(f"\n=== {setup.upper()} ===", flush=True)
    conditions, cond_file = load_signal_conditions(setup)
    if not conditions:
        print(f"  no signal conditions -> SKIP", flush=True)
        return None
    exit_cond = load_exit_cond(setup)
    if not exit_cond:
        print(f"  no exit condition -> SKIP", flush=True)
        return None
    direction = load_direction(setup)
    if not direction:
        print(f"  no direction in setups table -> SKIP", flush=True)
        return None
    examples = load_examples(setup)
    if not examples:
        print(f"  no examples -> SKIP", flush=True)
        return None
    print(f"  conditions={len(conditions)} from {cond_file}", flush=True)
    print(f"  exit: {exit_cond.get('expression')} {exit_cond.get('direction')} {exit_cond.get('threshold')}", flush=True)
    print(f"  direction={direction}, n_examples={len(examples)}", flush=True)

    # Resolve examples to (ticker, signal_idx) = (ticker, entry_idx - 1)
    example_signal_set = {}
    example_bugs = []
    ex_tickers = set()
    for tk, ed in examples:
        ex_tickers.add(tk)
        cd, _ = expr_cache.get_ticker(tk)
        if cd is None:
            example_bugs.append((tk, ed, "no_expr_cache"))
            continue
        dmap = {str(d)[:10]: i for i, d in enumerate(cd)}
        if ed not in dmap:
            example_bugs.append((tk, ed, "entry_date_not_in_expr_cache"))
            continue
        eidx = dmap[ed]
        sidx = eidx - 1
        if sidx < 0:
            example_bugs.append((tk, ed, "signal_idx<0"))
            continue
        example_signal_set[(tk, sidx)] = (ed, eidx)

    # Scan signals
    raw = scan_signals(setup, conditions, universe_cache, expr_cache, ex_tickers)

    # Identify which examples actually fire on entry-1 (condition-match examples)
    for (tk, sidx), (ed, eidx) in list(example_signal_set.items()):
        if sidx not in raw.get(tk, []):
            example_bugs.append((tk, ed, "conditions_dont_fire_on_entry-1"))
            # Keep in example_signal_set for info but V1 won't see it
    n_ex_condmatch = sum(1 for k in example_signal_set if k[1] in raw.get(k[0], []))
    print(f"  examples that fire on entry-1: {n_ex_condmatch}/{len(examples)}", flush=True)

    # Build clusters
    clusters = build_clusters(raw, example_signal_set)
    print(f"  clusters: {len(clusters)}", flush=True)

    # Apply V1 on each cluster
    print(f"  classifying V1...", flush=True)
    exit_col = expr_cache.expr_index(exit_cond["expression"])
    if exit_col is None:
        print(f"  exit expr not in expr cache -> SKIP", flush=True)
        return None

    # Preload per-ticker aligned OHLCV + exit series
    ticker_data = {}
    for tk in {c["ticker"] for c in clusters}:
        df = universe_cache.get(tk)
        if df is None:
            continue
        cd, cdata = expr_cache.get_ticker(tk)
        if cd is None:
            continue
        df_t, _ = align_ohlcv(df, cd)
        if df_t is None:
            continue
        ticker_data[tk] = (df_t, cdata[:, exit_col])

    exit_dir = exit_cond["direction"]
    exit_thresh = float(exit_cond["threshold"])

    n_win = n_loss = 0
    n_ex_seen = 0  # examples that classified (matched a cluster and had data)
    n_ex_win_v1 = 0  # examples V1 called WIN naturally (no override needed)
    n_ex_loss_v1 = 0
    example_losses_detail = []

    reason_counter = Counter()
    for c in clusters:
        tk = c["ticker"]
        sig = c["rightmost_idx"]
        is_example = (tk, sig) in example_signal_set
        if tk not in ticker_data:
            cls, reason = ("WIN" if is_example else "LOSS"), "no_data"
        else:
            df_t, exit_series = ticker_data[tk]
            cls, reason, _ex_b, _stop_b = classify_v1(
                {"rightmost_idx": sig}, df_t, exit_series, exit_dir, exit_thresh, direction
            )
        reason_counter[reason] += 1
        if cls == "WIN":
            n_win += 1
        else:
            n_loss += 1
        if is_example:
            n_ex_seen += 1
            if cls == "WIN":
                n_ex_win_v1 += 1
            else:
                n_ex_loss_v1 += 1
                ed, eidx = example_signal_set[(tk, sig)]
                example_losses_detail.append((tk, ed, reason))

    # Non-example stats
    n_non_ex = sum(1 for c in clusters if (c["ticker"], c["rightmost_idx"]) not in example_signal_set)
    n_non_ex_win = 0
    for c in clusters:
        tk = c["ticker"]
        sig = c["rightmost_idx"]
        if (tk, sig) in example_signal_set:
            continue
        if tk not in ticker_data:
            # non-examples with no data -> LOSS in count above
            continue
        df_t, exit_series = ticker_data[tk]
        cls, _r, _e, _s = classify_v1(
            {"rightmost_idx": sig}, df_t, exit_series, exit_dir, exit_thresh, direction
        )
        if cls == "WIN":
            n_non_ex_win += 1

    result = {
        "setup": setup,
        "direction": direction,
        "n_conditions": len(conditions),
        "exit_expr": exit_cond.get("expression"),
        "exit_thresh": exit_cond.get("threshold"),
        "exit_dir": exit_cond.get("direction"),
        "n_examples_total": len(examples),
        "n_example_bugs": len(example_bugs),
        "n_examples_condmatch": n_ex_condmatch,
        "n_examples_seen_in_clusters": n_ex_seen,
        "n_examples_V1_win": n_ex_win_v1,
        "n_examples_V1_loss_needs_override": n_ex_loss_v1,
        "n_clusters": len(clusters),
        "n_clusters_V1_win": n_win,
        "n_clusters_V1_loss": n_loss,
        "non_ex_win_rate_pct": round(100 * n_non_ex_win / max(n_non_ex, 1), 2) if n_non_ex else None,
        "top_reasons": dict(reason_counter.most_common(8)),
        "example_losses_detail": example_losses_detail[:20],
    }
    print(json.dumps(result, indent=2, default=str), flush=True)
    return result


def main():
    print("Loading universe OHLCV...", flush=True)
    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  {len(universe):,} tickers", flush=True)
    print("Loading expression cache...", flush=True)
    expr_cache = ExprSeriesCache()
    print(f"  {expr_cache.n_expressions} expressions", flush=True)

    rows = []
    for setup in ["dtss", "htf", "bf", "base"]:
        r = run_setup(setup, universe, expr_cache)
        if r is not None:
            rows.append(r)

    # Save
    out = os.path.join(RESEARCH_OUT, "v1_allsetups.json")
    with open(out, "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\nWrote {out}", flush=True)

    # Print summary table
    print("\n===== SUMMARY =====")
    print(f"{'setup':6s} {'dir':5s} {'n_ex':>5s} {'bugs':>5s} {'seen':>5s} {'v1_win':>7s} {'v1_loss':>8s} {'clusters':>9s} {'non_ex_wr%':>11s}")
    for r in rows:
        print(f"{r['setup']:6s} {r['direction']:5s} "
              f"{r['n_examples_total']:5d} "
              f"{r['n_example_bugs']:5d} "
              f"{r['n_examples_seen_in_clusters']:5d} "
              f"{r['n_examples_V1_win']:7d} "
              f"{r['n_examples_V1_loss_needs_override']:8d} "
              f"{r['n_clusters']:9d} "
              f"{r['non_ex_win_rate_pct']!s:>11s}")


if __name__ == "__main__":
    main()
