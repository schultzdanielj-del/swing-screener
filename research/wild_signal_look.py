"""Open the tape on 10 random wild signals per setup. No rule-fitting — just look."""
from __future__ import annotations

import os, sys, json, pickle, random, sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
WORKTREE = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
OUT_TXT = os.path.join(WORKTREE, "research", "wild_signal_tapes.txt")

# Let the grinder code import
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
from expr_cache_builder import ExprSeriesCache  # noqa: E402

PYRAMIDS = {
    "htf":    "pyramid_htf_mp_sig534_pk5_20260417_231416.json",
    "bf":     "pyramid_bf_mp_sig530_pk5_20260417_234303.json",
    "base":   "pyramid_base_mp_sig646_pk4_20260417_235640.json",
    "dtss":   "pyramid_dtss_mp_sig1369_pk11_20260417_232945.json",
    "3-4db":  "pyramid_3-4db_mp_sig653_pk11_20260418_000904.json",
}
BREAKOUT = {"htf", "bf", "base"}
FADE = {"dtss", "3-4db"}
N_SAMPLES = 10
PRIOR_BARS = 3
FWD_BARS = 20
SEED = 42


def load_pyramid(path):
    with open(path, 'r') as f:
        d = json.load(f)
    tr = d["tier_results"]
    # "Last tier" = last one with final_signals per insertion order.
    last_key = None
    for k, v in tr.items():
        if isinstance(v, dict) and "final_signals" in v:
            last_key = k
    if last_key is None:
        return []
    return tr[last_key]["final_signals"]


def adr14_at(df, idx):
    """14-bar SMA of (high-low) ending at idx. Matches pyramid_grinder fallback."""
    lo = max(0, idx - 13)
    hi_arr = df["high"].values[lo:idx + 1]
    lo_arr = df["low"].values[lo:idx + 1]
    if len(hi_arr) == 0:
        return float('nan')
    return float(np.mean(hi_arr - lo_arr))


def get_examples_for_setup(setup):
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT ticker, entry_date FROM examples WHERE setup_type=?",
        (setup,)
    ).fetchall()
    conn.close()
    return {(t, d) for t, d in rows}


def load_earnings():
    conn = sqlite3.connect(DB)
    rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    conn.close()
    by_t = defaultdict(list)
    for t, d in rows:
        by_t[t].append(d)
    for t in by_t:
        by_t[t].sort()
    return by_t


def next_ern(em, ticker, after):
    ds = em.get(ticker, [])
    for d in ds:
        if d > after:
            return d
    return None


def dates_as_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime('%Y-%m-%d').values
    return np.array([str(d)[:10] for d in df["date"].values])


def main():
    random.seed(SEED)

    # Sanity: OHLCV count
    ohlcv_path = os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl")
    print(f"OHLCV cache path: {ohlcv_path}", flush=True)
    with open(ohlcv_path, 'rb') as f:
        universe = pickle.load(f)
    print(f"Universe count: {len(universe)} tickers", flush=True)
    if len(universe) < 11000:
        print(f"FAIL: count {len(universe)} below 11000, stopping", flush=True)
        sys.exit(1)

    em = load_earnings()
    print(f"Earnings map: {len(em)} tickers", flush=True)

    # Expression cache — for ADR14 + level_above1_distance
    ec = ExprSeriesCache()
    if not ec.is_valid():
        print("FAIL: expression cache invalid, stopping", flush=True)
        sys.exit(1)
    adr_col = ec.expr_index("adr14")
    lsp_col = ec.expr_index("level_above1_distance")
    print(f"Expr cache: {ec.n_expressions} expressions, adr14 col={adr_col}, level_above1_distance col={lsp_col}", flush=True)

    lines = []
    lines.append(f"WILD SIGNAL TAPES — seed={SEED}, {N_SAMPLES} per setup, forward={FWD_BARS} bars")

    for setup, jf in PYRAMIDS.items():
        signals = load_pyramid(os.path.join(CACHE_DIR, jf))
        is_fade = setup in FADE
        klass = "FADE" if is_fade else "BREAKOUT"
        examples = get_examples_for_setup(setup)

        # Exclude signals that match examples (entry_date = signal date + 1 would be in DB),
        # but simplest: exclude (ticker, date+1) that are examples. Since entry = signal+1
        # by DB convention, the example's signal bar itself isn't in the examples table
        # (entry_date is). We'll just note if a sample's ticker has any example nearby.
        pool = [s for s in signals]
        if len(pool) < N_SAMPLES:
            print(f"WARN: {setup} has only {len(pool)} signals", flush=True)
        sampled = random.sample(pool, min(N_SAMPLES, len(pool)))

        lines.append("")
        lines.append("=" * 78)
        lines.append(f"{setup.upper()} ({klass}) — {len(signals)} total signals — {len(sampled)} samples")
        lines.append("=" * 78)

        for s in sampled:
            ticker, date = s["ticker"], s["date"]
            df = universe.get(ticker)
            if df is None or len(df) < 50:
                lines.append(f"\n-- {ticker} {date}: no OHLCV")
                continue
            ds = dates_as_str(df)
            matches = np.where(ds == date)[0]
            if len(matches) == 0:
                lines.append(f"\n-- {ticker} {date}: date missing from OHLCV")
                continue
            sig_i = int(matches[0])

            # OHLCV signal bar
            so, sh, sl, sc = (float(df[k].iloc[sig_i]) for k in ("open", "high", "low", "close"))

            # ADR14 + LSP from expr cache
            adr14 = float('nan')
            lsp_dist = float('nan')
            lsp_price = float('nan')
            cols_to_load = [c for c in (adr_col, lsp_col) if c is not None]
            if cols_to_load:
                expr_dates, expr_data = ec.get_ticker_series(ticker, cols_to_load)
                if expr_dates is not None:
                    ed_str = np.array([str(d)[:10] for d in expr_dates])
                    em_matches = np.where(ed_str == date)[0]
                    if len(em_matches) > 0:
                        ei = int(em_matches[0])
                        # cols_to_load may have 1 or 2 entries; figure indices
                        col_map = {}
                        for ci, orig_col in enumerate(cols_to_load):
                            if orig_col == adr_col:
                                col_map['adr14'] = ci
                            if orig_col == lsp_col:
                                col_map['lsp'] = ci
                        if 'adr14' in col_map:
                            adr14 = float(expr_data[ei, col_map['adr14']])
                        if 'lsp' in col_map:
                            lsp_dist = float(expr_data[ei, col_map['lsp']])
                        if is_fade and not np.isnan(adr14) and not np.isnan(lsp_dist):
                            lsp_price = sc + lsp_dist * adr14

            # Earnings
            ern = next_ern(em, ticker, date)
            ern_str = f"next_earnings={ern}" if ern else "no_earnings"

            # Has any labelled example on this ticker?
            example_notes = [d for (t, d) in examples if t == ticker]
            ex_note = f"  (examples on this ticker: {', '.join(example_notes)})" if example_notes else ""

            lines.append("")
            lines.append(f"── {ticker} {date} [{setup}] ──{ex_note}")
            lines.append(f"  Signal: O={so:.2f} H={sh:.2f} L={sl:.2f} C={sc:.2f}  ADR14={adr14:.2f}  {ern_str}")
            if is_fade:
                if not np.isnan(lsp_price):
                    lines.append(f"  LSP_above1: dist={lsp_dist:+.2f} ADR  price={lsp_price:.2f}  "
                                 f"(breach needs high > {lsp_price:.2f}; fail needs close < {lsp_price:.2f})")
                else:
                    lines.append(f"  LSP_above1: not available (dist nan)")

            # Prior bars
            for k in range(-PRIOR_BARS, 0):
                bi = sig_i + k
                if bi < 0:
                    continue
                d = ds[bi]
                o, h, l, c = (float(df[kk].iloc[bi]) for kk in ("open", "high", "low", "close"))
                lines.append(f"  [{k:+2d}] {d}  O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}")

            # Signal bar line
            lines.append(f"  [ 0] {date}  O={so:.2f} H={sh:.2f} L={sl:.2f} C={sc:.2f}  <-- SIGNAL")

            # Forward bars
            mech_first = None
            mech_note = ""
            end_i = min(sig_i + FWD_BARS, len(df) - 1)
            for k in range(1, end_i - sig_i + 1):
                bi = sig_i + k
                d = ds[bi]
                o, h, l, c = (float(df[kk].iloc[bi]) for kk in ("open", "high", "low", "close"))
                fl = []
                if not is_fade:
                    hit_high = h > sh
                    hit_low = c < sl
                    if hit_high:
                        fl.append("h>sigH")
                        if mech_first is None:
                            mech_first = k
                            fl.append("<-MECH")
                    if hit_low:
                        fl.append("c<sigL")
                else:
                    if not np.isnan(lsp_price):
                        breach = h > lsp_price
                        fail = c < lsp_price
                        clear = c > lsp_price
                        if breach:
                            fl.append("h>LSP")
                        if clear:
                            fl.append("c>LSP")
                        if breach and fail:
                            fl.append("B+F")
                            if mech_first is None:
                                mech_first = k
                                fl.append("<-MECH")
                at_ern = ern is not None and d >= ern
                if at_ern:
                    fl.append("[ERN]")
                flag = " ".join(fl)
                lines.append(f"  [+{k:2d}] {d}  O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}  {flag}")
                if at_ern:
                    mech_note = "  (earnings hit within window)"
                    break

            if mech_first is None:
                lines.append(f"  >> mechanic did NOT fire in {FWD_BARS} bars{mech_note}")
            else:
                lines.append(f"  >> mechanic first fired at offset +{mech_first}{mech_note}")

    # Write out
    os.makedirs(os.path.dirname(OUT_TXT), exist_ok=True)
    with open(OUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    print(f"\nWrote {len(lines)} lines to {OUT_TXT}", flush=True)


if __name__ == "__main__":
    main()
