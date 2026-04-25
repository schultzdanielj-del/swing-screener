"""Side-by-side example vs wild forward-tape comparison — CORRECT mechanic.

Stop-first framing per §2 c4/c5:
  stop_level = entry_candle_low  (always)
  max_entry  = min(entry_candle_low + 1*ADR, entry_candle_high)
  stop_hit   = any(low[entry+1..cap] < entry_candle_low)
  LOSS       = stop_hit
  WIN        = !stop_hit AND cap_close > max_entry       (clears worst-case fill)
  AMBIGUOUS  = !stop_hit AND stop_level < cap_close <= max_entry
  BE         = deferred (needs ratchet bar-N)

Entry convention:
  For examples, entry_bar_idx = signal_bar_idx + 1 (DB convention).
  For wild, entry_bar_idx = signal_bar_idx + 1 *provisional* — Lane-1 entry
  inference work locks the real rule later.

Selection bias flag:
  A+ winners are pre-curated into the example DB. Wild-only WR or median-winner
  metrics are biased low. We report discrimination (wild vs example separate)
  AND combined-pool (examples + wild together) for proper production reading.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
PANEL = os.path.join(WORKTREE, "research", "forward_tape_panel")
SETUPS = ["htf", "bf", "base"]


def enrich(scalars, ts, join_keys, universe_df_getter):
    """Derive entry-anchored features using entry_bar_idx = signal_bar_idx + 1.
    universe_df_getter(ticker) returns the OHLCV DataFrame for the ticker."""
    rows = []
    for _, s in scalars.iterrows():
        tk = s["ticker"]
        sig_idx = int(s["signal_bar_idx"])
        entry_idx = sig_idx + 1
        df = universe_df_getter(tk)
        if df is None or entry_idx >= len(df):
            continue
        adr = float(s["adr_at_signal"])
        entry_open = float(df["open"].values[entry_idx])
        entry_high = float(df["high"].values[entry_idx])
        entry_low = float(df["low"].values[entry_idx])
        entry_close = float(df["close"].values[entry_idx])

        stop_level = entry_low
        max_entry = min(entry_low + 1.0 * adr, entry_high)

        # From the panel timeseries, fetch forward bars from entry+1 (offset >= 2).
        key_filter = pd.Series([True] * len(ts))
        for k in join_keys:
            key_filter &= (ts[k] == s[k])
        sub = ts[key_filter].sort_values("offset")
        fwd = sub[sub["offset"] >= 2]  # offset 1 = entry bar; stop evaluation starts offset 2
        if len(fwd) == 0:
            stop_hit = False
            min_fwd_low = float("nan")
            cap_close = entry_close
            cap_off = 1
        else:
            min_fwd_low = float(fwd["l"].min())
            stop_hit = min_fwd_low < stop_level
            cap_row = fwd.iloc[-1]  # last forward bar
            cap_close = float(cap_row["c"])
            cap_off = int(cap_row["offset"])

        # Outcome
        if stop_hit:
            label = "LOSS"
        elif cap_close > max_entry:
            label = "WIN"
        elif cap_close > stop_level:
            label = "AMBIGUOUS"
        else:
            # cap_close below stop_level but stop_hit was False — contradiction unless
            # forward window terminated at entry bar. Classify as LOSS to be safe.
            label = "LOSS"

        rows.append({
            **{k: s[k] for k in join_keys},
            "ticker": tk,
            "setup": s["setup"],
            "signal_bar_idx": sig_idx,
            "entry_bar_idx": entry_idx,
            "signal_date": s.get("signal_date", None),
            "adr_at_signal": adr,
            "entry_open": entry_open,
            "entry_high": entry_high,
            "entry_low": entry_low,
            "entry_close": entry_close,
            "stop_level": stop_level,
            "max_entry": max_entry,
            "entry_range_adr": (entry_high - entry_low) / adr,
            "stop_distance_to_candle_high_adr": (entry_high - stop_level) / adr,
            "min_fwd_low": min_fwd_low,
            "cap_close": cap_close,
            "cap_offset": cap_off,
            "stop_hit": bool(stop_hit),
            "cap_close_vs_stop_adr": (cap_close - stop_level) / adr,
            "cap_close_vs_max_entry_adr": (cap_close - max_entry) / adr,
            "adverse_excursion_adr": (min_fwd_low - stop_level) / adr if not np.isnan(min_fwd_low) else float("nan"),
            "label": label,
        })
    return pd.DataFrame(rows)


def load_universe():
    import pickle
    with open(r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl", "rb") as f:
        return pickle.load(f)


def report(setup, ex, wd):
    print(f"\n============== {setup.upper()} ==============")
    print(f"  examples: n={len(ex)}   wild: n={len(wd)}   combined: n={len(ex)+len(wd)}")

    def tally(df, pop):
        c = df["label"].value_counts().to_dict()
        W = c.get("WIN", 0); L = c.get("LOSS", 0); A = c.get("AMBIGUOUS", 0)
        n = len(df)
        print(f"    {pop:<10} W={W:>3}/{n:<3} ({W/max(n,1):.0%})  "
              f"L={L:>3}/{n:<3} ({L/max(n,1):.0%})  "
              f"A={A:>3}/{n:<3} ({A/max(n,1):.0%})")

    print("  labels by population:")
    tally(ex, "examples")
    tally(wd, "wild")
    combined = pd.concat([ex, wd], ignore_index=True)
    tally(combined, "combined")

    # Features by label on WILD
    if len(wd) > 0:
        print("  wild by-label feature quartiles (p25/p50/p75):")
        for lbl in ("WIN", "LOSS", "AMBIGUOUS"):
            sub = wd[wd["label"] == lbl]
            if len(sub) == 0:
                continue
            def q(x):
                vals = x.dropna().values
                if len(vals) == 0: return [None, None, None]
                return [round(float(np.quantile(vals, p)), 2) for p in (0.25, 0.5, 0.75)]
            print(f"    {lbl:<10} n={len(sub):<3}  "
                  f"cap_close_vs_stop_adr={q(sub['cap_close_vs_stop_adr'])}  "
                  f"cap_close_vs_max_entry_adr={q(sub['cap_close_vs_max_entry_adr'])}  "
                  f"adverse_excursion_adr={q(sub['adverse_excursion_adr'])}  "
                  f"entry_range_adr={q(sub['entry_range_adr'])}")

    # Example flags — any stop_hit on examples is a red flag
    ex_loss = ex[ex["label"] == "LOSS"]
    ex_ambig = ex[ex["label"] == "AMBIGUOUS"]
    if len(ex_loss) > 0:
        print(f"  !! examples labeled LOSS ({len(ex_loss)}):")
        for _, r in ex_loss.iterrows():
            print(f"      {r['ticker']:<6} sig={r['signal_date']}  stop_level={r['stop_level']:.2f}  "
                  f"min_fwd_low={r['min_fwd_low']:.2f}  adverse_exc={r['adverse_excursion_adr']:+.2f}ADR")
    if len(ex_ambig) > 0:
        print(f"  !! examples labeled AMBIGUOUS ({len(ex_ambig)}):")
        for _, r in ex_ambig.iterrows():
            print(f"      {r['ticker']:<6} sig={r['signal_date']}  cap_close_vs_max_entry={r['cap_close_vs_max_entry_adr']:+.2f}ADR  "
                  f"cap_close_vs_stop={r['cap_close_vs_stop_adr']:+.2f}ADR")

    # Wild WIN-candidates and LOSS counts
    wd_win = wd[wd["label"] == "WIN"]
    wd_loss = wd[wd["label"] == "LOSS"]
    wd_ambig = wd[wd["label"] == "AMBIGUOUS"]

    def show(name, df, sort_col, ascending):
        print(f"  wild {name} (n={len(df)}, top 6):")
        for _, r in df.sort_values(sort_col, ascending=ascending).head(6).iterrows():
            print(f"      {r['ticker']:<6} sig={r['signal_date']}  "
                  f"stop={r['stop_level']:.2f} max_entry={r['max_entry']:.2f}  "
                  f"cap_close={r['cap_close']:.2f}  "
                  f"cap_vs_stop={r['cap_close_vs_stop_adr']:+.2f}ADR  "
                  f"cap_vs_max_entry={r['cap_close_vs_max_entry_adr']:+.2f}ADR  "
                  f"adv_exc={r['adverse_excursion_adr']:+.2f}ADR")

    if len(wd_win) > 0:
        show("WIN", wd_win, "cap_close_vs_max_entry_adr", False)
    if len(wd_ambig) > 0:
        show("AMBIGUOUS", wd_ambig, "cap_close_vs_max_entry_adr", False)
    if len(wd_loss) > 0:
        show("LOSS", wd_loss, "adverse_excursion_adr", True)


def main():
    universe = load_universe()
    def getter(tk): return universe.get(tk)

    for setup in SETUPS:
        ex_s = pd.read_pickle(os.path.join(PANEL, f"{setup}_scalars.pkl"))
        ex_ts = pd.read_pickle(os.path.join(PANEL, f"{setup}_timeseries.pkl"))
        wd_s = pd.read_pickle(os.path.join(PANEL, f"{setup}_wild_scalars.pkl"))
        wd_ts = pd.read_pickle(os.path.join(PANEL, f"{setup}_wild_timeseries.pkl"))

        ex = enrich(ex_s, ex_ts, ["ticker", "entry_date"], getter)
        wd = enrich(wd_s, wd_ts, ["ticker", "cluster_id"], getter)

        report(setup, ex, wd)


if __name__ == "__main__":
    main()
