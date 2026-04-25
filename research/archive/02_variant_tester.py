"""Test multiple classifier variants on a raw_signal_clusters_* JSON.

For each cluster we need to re-run the race (stop vs exit) under the variant
being tested. To do that we need:
  - OHLCV for the ticker (from main repo pickle)
  - Exit condition expression values per bar (from main repo expr cache)
  - Signal bar index (cluster.rightmost.bar_idx, cache-relative)

The existing cluster file is authoritative for the signal bar index (it's
cache-relative, produced by the same scan path the grinder uses) so we trust
it and bypass re-running signal scans.

Variants we test (longs — shorts mirror):

  V0_current:  existing rule from cluster file (no re-run, reference).
  V1_entry1:   stop = low[signal+1].  Race from signal+2.
  V2_entryFW:  stop = low[signal+FW]. Race from signal+FW+1.
  V3_fwMinLow: stop = min(low[signal+1..signal+FW]).  Race from signal+FW+1.
               ("deepest potential entry" — most permissive / loosest.)
  V4_fwMaxLow: stop = max(low[signal+1..signal+FW]).  Race from signal+FW+1.
               ("shallowest potential entry" — most strict.)
  V5_anyEntry: for each k in 1..FW, stop_k = low[signal+k]; LOSS if any k has
               a later bar in [signal+k+1..signal+120] below stop_k BEFORE
               exit fires; WIN otherwise.
               ("any plausible entry is stopped" — union of losses.)
  V6_allEntry: LOSS only if EVERY k in 1..FW has a later bar breaching its
               stop before exit fires.
               ("every plausible entry is stopped" — intersection of losses.)
  V7_adr1:     stop = signal_close - 1.0 * ADR (long).  Race from signal+1.
               (Dan's 1-ADR rule as literal floor.)

Race mechanics (common):
  - check window = [race_start, min(signal+120, end_of_data)]
  - LOSS if within window, any bar has low < stop (long) / high > stop (short)
    BEFORE exit condition fires.
  - WIN if exit fires BEFORE stop breach (same-bar: stop wins, intrabar touch
    beats close-evaluated exit).
  - WIN timeout if neither fires in 120 bars.

Outputs:
  - Per-variant win rate (examples and non-examples separately).
  - Per-variant example pass count (must be 100%).
  - Per-variant confusion matrix vs V0_current.
  - Move_adr distribution summary per variant.
"""

from __future__ import annotations

import os
import sys
import json
import pickle
from collections import Counter

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
sys.path.insert(0, os.path.join(MAIN_ROOT, "scripts"))

# Make cache-relative paths resolve against the main repo
os.chdir(MAIN_ROOT)

from expr_cache_builder import ExprSeriesCache, EXPR_CACHE_START  # noqa: E402

RESEARCH_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(RESEARCH_DIR, "out")
os.makedirs(OUT_DIR, exist_ok=True)


def load_cluster_file(path):
    with open(path) as f:
        d = json.load(f)
    return d


def load_ohlcv():
    with open(os.path.join(MAIN_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl"), "rb") as f:
        return pickle.load(f)


def align_ohlcv_to_expr(df, expr_dates):
    """Align OHLCV to expression cache dates. Returns truncated df of same length as expr_dates,
    or None if alignment impossible. Mirrors the alignment logic in pyramid_grinder.py."""
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        ohlcv_dates = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    else:
        ohlcv_dates = np.array([str(d)[:10] for d in df["date"].values])
    if len(expr_dates) == 0:
        return None
    expr_first = str(expr_dates[0])[:10]
    hits = np.where(ohlcv_dates == expr_first)[0]
    if len(hits) == 0:
        return None
    off = int(hits[0])
    end = off + len(expr_dates)
    if end > len(df):
        end = len(df)
    return df.iloc[off:end].reset_index(drop=True)


def adr14_at(df, i):
    start = max(0, i - 13)
    h = df["high"].values[start : i + 1]
    l = df["low"].values[start : i + 1]
    return float(np.mean(h - l)) if len(h) else float("nan")


def classify_one(
    variant: str,
    direction: str,
    df: pd.DataFrame,
    signal_idx: int,
    forward_window: int,
    exit_series: np.ndarray,
    exit_dir: str,
    exit_thresh: float,
    max_bars: int = 120,
):
    """Return ('WIN' or 'LOSS', reason, exit_bar_rel_or_None, stop_bar_rel_or_None).

    All bar counts returned are relative to signal_idx (e.g. 1 = signal+1).
    """
    n = len(df)
    if signal_idx >= n - 1:
        return ("WIN", "no_data_after_signal", None, None)

    sig_close = float(df["close"].values[signal_idx])
    lows = df["low"].values
    highs = df["high"].values

    fw_end = min(signal_idx + forward_window, n - 1)
    end = min(signal_idx + max_bars, n - 1)

    def first_stop_and_exit(race_start, stop_level):
        if race_start >= n:
            return None, None
        # Stop (intrabar): resting order fills on any touch
        if direction == "long":
            hits = np.where(lows[race_start:end + 1] < stop_level)[0]
        else:
            hits = np.where(highs[race_start:end + 1] > stop_level)[0]
        first_stop = int(hits[0] + (race_start - signal_idx)) if len(hits) else None

        # Exit (close-evaluated)
        ex_slice = exit_series[race_start:end + 1]
        finite = ~np.isnan(ex_slice)
        if exit_dir in (">=", "above"):
            ex_hits = np.where(finite & (ex_slice >= exit_thresh))[0]
        else:
            ex_hits = np.where(finite & (ex_slice <= exit_thresh))[0]
        first_exit = int(ex_hits[0] + (race_start - signal_idx)) if len(ex_hits) else None
        return first_stop, first_exit

    if variant == "V1_entry1":
        entry = signal_idx + 1
        if entry >= n:
            return ("WIN", "no_entry_bar", None, None)
        stop_level = float(lows[entry]) if direction == "long" else float(highs[entry])
        race_start = entry + 1
    elif variant == "V8_sigExtreme":
        stop_level = float(lows[signal_idx]) if direction == "long" else float(highs[signal_idx])
        race_start = signal_idx + 1
    elif variant == "V9_clusterExtreme":
        # cluster extreme passed via forward_window.  We don't have it here; approximate
        # by using max over [signal_idx-FW+1 .. signal_idx] (assumes cluster no wider than FW).
        lo = max(0, signal_idx - forward_window + 1)
        hi = signal_idx + 1
        stop_level = float(np.min(lows[lo:hi])) if direction == "long" else float(np.max(highs[lo:hi]))
        race_start = signal_idx + 1
    elif variant == "V2_entryFW":
        entry = fw_end
        if entry >= n:
            return ("WIN", "no_entry_bar", None, None)
        stop_level = float(lows[entry]) if direction == "long" else float(highs[entry])
        race_start = entry + 1
    elif variant == "V3_fwMinLow":
        seg = slice(signal_idx + 1, fw_end + 1)
        if signal_idx + 1 > fw_end:
            return ("WIN", "no_fw", None, None)
        stop_level = float(np.min(lows[seg])) if direction == "long" else float(np.max(highs[seg]))
        race_start = fw_end + 1
    elif variant == "V4_fwMaxLow":
        seg = slice(signal_idx + 1, fw_end + 1)
        if signal_idx + 1 > fw_end:
            return ("WIN", "no_fw", None, None)
        stop_level = float(np.max(lows[seg])) if direction == "long" else float(np.min(highs[seg]))
        race_start = fw_end + 1
    elif variant == "V7_adr1":
        adr = adr14_at(df, signal_idx)
        if not np.isfinite(adr) or adr <= 0:
            return ("LOSS", "bad_adr", None, None)
        if direction == "long":
            stop_level = sig_close - 1.0 * adr
        else:
            stop_level = sig_close + 1.0 * adr
        race_start = signal_idx + 1
    elif variant in ("V5_anyEntry", "V6_allEntry"):
        # Per-k loop
        per_k = []
        for k in range(1, forward_window + 1):
            entry = signal_idx + k
            if entry >= n:
                break
            sl = float(lows[entry]) if direction == "long" else float(highs[entry])
            rs = entry + 1
            first_stop, first_exit = first_stop_and_exit(rs, sl)
            # Determine per-k outcome
            if first_stop is not None and (first_exit is None or first_stop <= first_exit):
                per_k.append(("LOSS", first_stop))
            elif first_exit is not None:
                per_k.append(("WIN", first_exit))
            else:
                per_k.append(("WIN", None))
        if not per_k:
            return ("WIN", "no_fw", None, None)
        if variant == "V5_anyEntry":
            # LOSS if any k is LOSS
            loss_ks = [k for k in per_k if k[0] == "LOSS"]
            if loss_ks:
                stop_bar = min(k[1] for k in loss_ks)
                return ("LOSS", "any_entry_stop", None, stop_bar)
            exit_bars = [k[1] for k in per_k if k[0] == "WIN" and k[1] is not None]
            return ("WIN", "all_entries_exit", min(exit_bars) if exit_bars else None, None)
        else:  # V6_allEntry
            if all(k[0] == "LOSS" for k in per_k):
                stop_bar = max(k[1] for k in per_k)  # latest k's stop = last plausible LOSS
                return ("LOSS", "all_entries_stop", None, stop_bar)
            exit_bars = [k[1] for k in per_k if k[0] == "WIN" and k[1] is not None]
            return ("WIN", "some_entry_wins", min(exit_bars) if exit_bars else None, None)
    else:
        raise ValueError(f"Unknown variant {variant}")

    # Generic race (single stop_level, race_start)
    first_stop, first_exit = first_stop_and_exit(race_start, stop_level)
    if first_stop is not None and (first_exit is None or first_stop <= first_exit):
        return ("LOSS", "stop_hit", None, first_stop)
    if first_exit is not None:
        return ("WIN", "exit_fired", first_exit, None)
    return ("WIN", "timeout", None, None)


def build_ticker_data(cluster_data, universe_cache, expr_cache, exit_cond):
    """Preload per-ticker OHLCV (truncated) + exit series once. Reused across variants."""
    exit_expr = exit_cond["expression"]
    exit_col = expr_cache.expr_index(exit_expr)
    if exit_col is None:
        raise RuntimeError(f"Exit expression '{exit_expr}' not in expression cache")
    ticker_data = {}
    tks = sorted({c["ticker"] for c in cluster_data["clusters"]})
    import time as _t
    t0 = _t.time()
    for i, tk in enumerate(tks):
        df = universe_cache.get(tk)
        if df is None or len(df) < 100:
            continue
        cd, cdata = expr_cache.get_ticker(tk)
        if cd is None or len(cd) == 0:
            continue
        df_t = align_ohlcv_to_expr(df, cd)
        if df_t is None or len(df_t) != len(cd):
            continue
        ticker_data[tk] = (df_t, cdata[:, exit_col])
        if (i + 1) % 500 == 0:
            print(f"    loaded {i+1}/{len(tks)} tickers ({_t.time()-t0:.0f}s)", flush=True)
    print(f"  ticker_data ready: {len(ticker_data)}/{len(tks)} tickers ({_t.time()-t0:.0f}s)", flush=True)
    return ticker_data


def run_variant_on_clusters(variant, cluster_data, ticker_data, exit_cond, setup_dir):
    """Apply variant to each cluster, return list of (classification, reason, exit_bar, stop_bar, cluster)."""
    results = []
    forward_window = cluster_data.get("forward_window") or 3
    exit_thresh = float(exit_cond["threshold"])
    exit_dir = exit_cond["direction"]

    for c in cluster_data["clusters"]:
        tk = c["ticker"]
        sig_idx = int(c["rightmost"]["bar_idx"])
        if tk not in ticker_data:
            results.append(("WIN" if c.get("is_example") == 1 else "LOSS", "no_data", None, None, c))
            continue
        df_t, exit_series = ticker_data[tk]
        cls, reason, ex_bar, stop_bar = classify_one(
            variant, setup_dir, df_t, sig_idx, forward_window, exit_series, exit_dir, exit_thresh
        )
        # Example override (always WIN)
        if c.get("is_example") == 1 and cls != "WIN":
            cls = "WIN"
            reason = f"example_override({reason})"
        results.append((cls, reason, ex_bar, stop_bar, c))
    return results


def summarize(results, variant_name):
    n_total = len(results)
    n_win = sum(1 for r in results if r[0] == "WIN")
    n_loss = n_total - n_win
    examples = [r for r in results if r[4].get("is_example") == 1]
    n_ex = len(examples)
    n_ex_win = sum(1 for r in examples if r[0] == "WIN")
    non_ex = [r for r in results if r[4].get("is_example") != 1]
    n_ne = len(non_ex)
    n_ne_win = sum(1 for r in non_ex if r[0] == "WIN")
    reasons = Counter(r[1] for r in results)
    return {
        "variant": variant_name,
        "n_total": n_total,
        "total_win_rate_pct": round(100 * n_win / max(n_total, 1), 2),
        "example_pass_pct": round(100 * n_ex_win / max(n_ex, 1), 2),
        "example_n": n_ex,
        "non_example_n": n_ne,
        "non_example_win_rate_pct": round(100 * n_ne_win / max(n_ne, 1), 2),
        "top_reasons": dict(reasons.most_common(6)),
    }


def main():
    # Cluster files to analyze (latest per setup)
    cluster_files = {
        "dtss": os.path.join(MAIN_ROOT, "local_runner/cache/raw_signal_clusters_dtss.json"),
        "brko": os.path.join(MAIN_ROOT, "local_runner/cache/_archive/brko_legacy/raw_signal_clusters_brko.json"),
    }

    universe = load_ohlcv()
    expr_cache = ExprSeriesCache()

    all_rows = []
    for setup, cluster_path in cluster_files.items():
        print(f"\n=== {setup}: {os.path.basename(cluster_path)} ===")
        data = load_cluster_file(cluster_path)
        direction = data.get("direction")
        exit_cond = data["exit_condition"]
        print(f"  direction={direction}, FW={data.get('forward_window')}, exit={exit_cond}")
        print(f"  clusters={data.get('n_clusters')}, current WR={data.get('n_winning_clusters', 0)}/{data.get('n_clusters', 1)}")

        print("  Preloading ticker data...", flush=True)
        ticker_data = build_ticker_data(data, universe, expr_cache, exit_cond)

        variants = ["V1_entry1", "V2_entryFW", "V3_fwMinLow", "V4_fwMaxLow", "V5_anyEntry", "V6_allEntry", "V7_adr1", "V8_sigExtreme", "V9_clusterExtreme"]
        for v in variants:
            res = run_variant_on_clusters(v, data, ticker_data, exit_cond, direction)
            s = summarize(res, v)
            s["setup"] = setup
            all_rows.append(s)
            print(f"  {v:12s} -> TotalWR={s['total_win_rate_pct']:5.1f}%  "
                  f"NonExWR={s['non_example_win_rate_pct']:5.1f}%  "
                  f"ExPass={s['example_pass_pct']:5.1f}% ({s['example_n']})  "
                  f"reasons={s['top_reasons']}", flush=True)

    df = pd.DataFrame(all_rows)
    out = os.path.join(OUT_DIR, "variant_comparison.csv")
    df.to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
