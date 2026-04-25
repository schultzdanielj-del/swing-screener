"""BE-ratchet sweep — direction-aware per §15.3.

Longs (breakouts):
  stop_level  = entry_candle_low
  bound_entry = min(entry_low + 1*ADR, entry_high)   # max_entry
  stop hit    = forward bar low < stop_level
  ratchet arm = prior close > entry_close; on arm, stop → entry_close

Shorts (fades):
  stop_level  = entry_candle_high
  bound_entry = max(entry_high - 1*ADR, entry_low)   # min_entry
  stop hit    = forward bar high > stop_level
  ratchet arm = prior close < entry_close; on arm, stop → entry_close

For each setup, sweeps N ∈ [1, N_MAX] and reports:
  - example LOSS/BE/WIN/AMBIGUOUS at each N
  - wild pool distribution at each N
  - combined distribution at each N
  - N_0 per setup = smallest N dropping example LOSS to 0 with BE ≤ XPEV_TOLERANCE

Labels (direction-symmetric):
  LOSS      = stop hit while stop still at entry_candle_extreme
  BE        = stop hit while stop ratcheted to entry_candle_close
  WIN       = no stop hit AND cap_close on profit side of bound_entry
  AMBIGUOUS = no stop hit AND cap_close between stop_level and bound_entry (spec
              catch-all; expected empty or near-empty under any reasonable data).
"""
from __future__ import annotations

import os
import pickle
import numpy as np
import pandas as pd

WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener-win-loss-classifier"
PANEL = os.path.join(WORKTREE, "research", "forward_tape_panel")
DIRECTION = {"htf": +1, "bf": +1, "base": +1, "dtss": -1, "3-4db": -1}
BREAKOUT_SETUPS = ["htf", "bf", "base"]
FADE_SETUPS = ["dtss", "3-4db"]
SETUPS = FADE_SETUPS  # fades only this session
N_MAX = 40
XPEV_TOLERANCE = 2  # allow up to this many example BEs; LOSS must reach 0.

UNIVERSE_PICKLE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener/local_runner/cache/universe_ohlcv_daily.pkl"


def classify_cluster(forward_rows, stop_level, entry_close, bound_entry, direction, N):
    """Direction-aware BE-ratchet classifier.

    direction = +1 (longs) or -1 (shorts).
    stop_level  = entry_candle_low (long) / entry_candle_high (short).
    bound_entry = max_entry (long) / min_entry (short).
    Stop hit:    l < stop (long) / h > stop (short).
    Ratchet arm: prior_close > entry_close (long) / < (short). On arm, stop → entry_close.
    Final cap:   last_close > bound_entry (long) / < (short) → WIN.
                 between stop and bound → AMBIGUOUS.
                 past stop without intraday hit → defensive LOSS.
    """
    stop = stop_level
    ratcheted = False
    last_close = None

    rows = list(forward_rows.iterrows())
    for idx, (_, r) in enumerate(rows):
        off = int(r["offset"])
        # Ratchet arming BEFORE stop check this bar. Prior bar's close = close at (off - 1).
        if not ratcheted and (off - 1) >= N:
            prior_close = rows[idx - 1][1]["c"] if idx > 0 else entry_close
            favorable = (prior_close > entry_close) if direction == +1 else (prior_close < entry_close)
            if favorable:
                stop = entry_close
                ratcheted = True
        # Stop hit — direction-aware
        hit = (r["l"] < stop) if direction == +1 else (r["h"] > stop)
        if hit:
            return ("BE" if ratcheted else "LOSS"), off, ratcheted, stop
        last_close = r["c"]

    if last_close is None:
        return "AMBIGUOUS", None, False, stop
    if direction == +1:
        if last_close > bound_entry:
            return "WIN", None, ratcheted, stop
        elif last_close > stop_level:
            return "AMBIGUOUS", None, ratcheted, stop
        else:
            return "LOSS", None, ratcheted, stop
    else:
        if last_close < bound_entry:
            return "WIN", None, ratcheted, stop
        elif last_close < stop_level:
            return "AMBIGUOUS", None, ratcheted, stop
        else:
            return "LOSS", None, ratcheted, stop


def enrich(scalars, ts, join_keys, universe, direction, N):
    rows = []
    for _, s in scalars.iterrows():
        tk = s["ticker"]; sig_idx = int(s["signal_bar_idx"])
        entry_idx = sig_idx + 1
        df = universe.get(tk)
        if df is None or entry_idx >= len(df):
            continue
        adr = float(s["adr_at_signal"])
        entry_high = float(df["high"].values[entry_idx])
        entry_low = float(df["low"].values[entry_idx])
        entry_close = float(df["close"].values[entry_idx])
        if direction == +1:
            stop_level = entry_low
            bound_entry = min(entry_low + 1.0 * adr, entry_high)
        else:
            stop_level = entry_high
            bound_entry = max(entry_high - 1.0 * adr, entry_low)

        key_filter = pd.Series([True] * len(ts))
        for k in join_keys:
            key_filter &= (ts[k] == s[k])
        sub = ts[key_filter].sort_values("offset")
        fwd = sub[sub["offset"] >= 2]

        label, hit_off, ratcheted, final_stop = classify_cluster(
            fwd, stop_level, entry_close, bound_entry, direction, N,
        )

        rows.append({
            **{k: s[k] for k in join_keys},
            "ticker": tk,
            "setup": s["setup"],
            "stop_level": stop_level, "entry_close": entry_close, "bound_entry": bound_entry,
            "label": label, "hit_off": hit_off, "ratcheted": ratcheted, "final_stop": final_stop,
        })
    return pd.DataFrame(rows)


def sweep_for_setup(setup, universe):
    direction = DIRECTION[setup]
    ex_s = pd.read_pickle(os.path.join(PANEL, f"{setup}_scalars.pkl"))
    ex_ts = pd.read_pickle(os.path.join(PANEL, f"{setup}_timeseries.pkl"))
    wd_s = pd.read_pickle(os.path.join(PANEL, f"{setup}_wild_scalars.pkl"))
    wd_ts = pd.read_pickle(os.path.join(PANEL, f"{setup}_wild_timeseries.pkl"))

    print(f"\n========== {setup.upper()}  (direction={direction:+d}) ==========")
    print(f"{'N':>3}  {'ex W/L/BE/A':>18}  {'wild W/L/BE/A':>22}  {'combined W/L/BE/A':>22}")
    print("-" * 72)

    best_N = None
    best_details = None
    all_rows = []
    for N in range(1, N_MAX + 1):
        ex = enrich(ex_s, ex_ts, ["ticker", "entry_date"], universe, direction, N)
        wd = enrich(wd_s, wd_ts, ["ticker", "cluster_id"], universe, direction, N)
        def cts(df):
            v = df["label"].value_counts().to_dict()
            return (v.get("WIN",0), v.get("LOSS",0), v.get("BE",0), v.get("AMBIGUOUS",0))
        ew,el,eb,ea = cts(ex); ww,wl,wb,wa = cts(wd)
        cw,cl,cb,ca = ew+ww, el+wl, eb+wb, ea+wa
        print(f"{N:>3}  {f'{ew}/{el}/{eb}/{ea}':>18}  {f'{ww}/{wl}/{wb}/{wa}':>22}  {f'{cw}/{cl}/{cb}/{ca}':>22}")
        all_rows.append((N, ew, el, eb, ea, ww, wl, wb, wa))
        if best_N is None and el == 0 and eb <= XPEV_TOLERANCE:
            best_N = N
            best_details = (ex, wd, ew, el, eb, ea, ww, wl, wb, wa)

    if best_N is None:
        print(f"  !! {setup}: no N in [1, {N_MAX}] drops example LOSS to 0 with BE <= {XPEV_TOLERANCE}.")
    else:
        print(f"\n  {setup} N_0 = {best_N}")
        ex, wd, ew, el, eb, ea, ww, wl, wb, wa = best_details
        print(f"    example:  W={ew} L={el} BE={eb} A={ea}  (n={len(ex)})")
        print(f"    wild:     W={ww} L={wl} BE={wb} A={wa}  (n={len(wd)})")
        cw, cl, cb, ca = ew+ww, el+wl, eb+wb, ea+wa
        tot = cw+cl+cb+ca
        print(f"    combined: W={cw} ({cw/tot:.0%})  L={cl} ({cl/tot:.0%})  BE={cb} ({cb/tot:.0%})  A={ca} ({ca/tot:.0%})  (n={tot})")
        ex_be = ex[ex["label"] == "BE"]
        if len(ex_be) > 0:
            print(f"    example BEs:")
            for _, r in ex_be.iterrows():
                print(f"      {r['ticker']:<6} entry_date={r.get('entry_date')}  hit_off={r['hit_off']}  entry_close={r['entry_close']:.2f}")
        ex_a = ex[ex["label"] == "AMBIGUOUS"]
        if len(ex_a) > 0:
            print(f"    example AMBIGUOUS:")
            for _, r in ex_a.iterrows():
                print(f"      {r['ticker']:<6} entry_date={r.get('entry_date')}  stop_level={r['stop_level']:.2f}  bound_entry={r['bound_entry']:.2f}")
    return best_N, best_details, all_rows


def main():
    with open(UNIVERSE_PICKLE, "rb") as f:
        universe = pickle.load(f)

    summary = {}
    all_sweeps = {}
    for setup in SETUPS:
        N_0, details, all_rows = sweep_for_setup(setup, universe)
        summary[setup] = N_0
        all_sweeps[setup] = all_rows

    print("\n========== SUMMARY ==========")
    for setup, N in summary.items():
        print(f"  {setup}: N_0 = {N}")

    # §15.8 failure-criteria explicit scan
    print("\n========== §15.8 FAILURE-CRITERIA SCAN ==========")
    for setup, rows in all_sweeps.items():
        ex_losses = [r[2] for r in rows]  # el
        ex_wins   = [r[1] for r in rows]  # ew
        min_loss  = min(ex_losses)
        max_win   = max(ex_wins)
        total_ex  = ex_wins[0] + ex_losses[0] + rows[0][3] + rows[0][4]
        win_rate_at_best = max_win / total_ex if total_ex else 0
        trigger_loss = (min_loss > 0)
        trigger_win  = (win_rate_at_best < 0.40)
        flags = []
        if trigger_loss:
            flags.append(f"LOSS>0 for all N (min={min_loss})")
        if trigger_win:
            flags.append(f"peak example WIN rate {win_rate_at_best:.1%} < 40%")
        status = "TRIGGER" if flags else "ok"
        print(f"  {setup}: {status}  min_ex_LOSS={min_loss}  peak_ex_WIN={max_win}/{total_ex}  {' | '.join(flags)}")


if __name__ == "__main__":
    main()
