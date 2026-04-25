"""Step 3-4 of classifier methodology: compare known winners vs losers.

Winners = 192 curated examples. Losers = every other firing-cluster in the
pyramid pop. No race, no signal+1, no invented labels.

Memory-safe: online running stats per feature per group (no vector
accumulation). Peak memory is one ticker's feature cache transiently plus
O(n_features) bookkeeping (~300 KB).

Outputs per class (breakout / fade):
  - cluster size distribution (winner vs loser)
  - forward-window envelope per class (winners only)
  - rightmost-offset distribution (entry - rightmost firing bar)
  - firing-bar shape (rng_adr / body_frac / close_pos) winner vs loser
  - top features by |sep_z| winner vs loser
"""
from __future__ import annotations

import os, sys, glob, json, pickle, sqlite3, gc
from collections import defaultdict
import numpy as np
import pandas as pd

MAIN_ROOT = r"C:/Users/Dan/Documents/ScanPerfect/swing-screener"
WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(MAIN_ROOT, "local_runner"))
os.chdir(MAIN_ROOT)
from expr_cache_builder import ExprSeriesCache  # noqa: E402

CACHE_DIR = os.path.join(MAIN_ROOT, "local_runner", "cache")
DB = os.path.join(MAIN_ROOT, "data", "scanperfect.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
os.makedirs(OUT_DIR, exist_ok=True)

FADE_SETUPS = {"dtss", "3-4db"}
ACTIVE = ["htf", "bf", "base", "dtss"]


def ohlcv_dates_str(df):
    if pd.api.types.is_datetime64_any_dtype(df["date"]):
        return pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").values
    return np.array([str(d)[:10] for d in df["date"].values])


def align_ohlcv_len(df, cd_len, first_cache_date):
    dates_full = ohlcv_dates_str(df)
    hits = np.where(dates_full == first_cache_date)[0]
    if len(hits) == 0: return None, None
    off = int(hits[0])
    end = min(off + cd_len, len(df))
    df_t = df.iloc[off:end].reset_index(drop=True)
    if len(df_t) != cd_len: return None, None
    return df_t, dates_full[off:end]


def latest_pyramid(setup):
    fs = glob.glob(os.path.join(CACHE_DIR, f"pyramid_{setup}_mp_sig*.json"))
    fs = [f for f in fs if "sig0_pk0" not in os.path.basename(f)]
    wt = [(json.load(open(f))["timestamp"], f) for f in fs]
    wt.sort()
    return wt[-1][1] if wt else None


def final_signals_from_pyramid(pyr):
    out = []
    for _, tr in pyr["tier_results"].items():
        if tr.get("final_signals"):
            out = tr["final_signals"]
    return out


class OnlineGroupStats:
    """Per-feature running: count (of finite values), sum, sum_of_squares.
    Memory: 3 × n_features × 8 bytes (float64).
    """
    def __init__(self, n_features):
        self.n  = np.zeros(n_features, dtype=np.int64)
        self.s1 = np.zeros(n_features, dtype=np.float64)
        self.s2 = np.zeros(n_features, dtype=np.float64)

    def update(self, vec):
        # vec is (n_features,) float32
        finite = np.isfinite(vec)
        vec_f = np.where(finite, vec.astype(np.float64), 0.0)
        self.n += finite.astype(np.int64)
        self.s1 += vec_f
        self.s2 += vec_f * vec_f

    def finalize(self):
        mean = np.where(self.n > 0, self.s1 / np.maximum(self.n, 1), np.nan)
        var  = np.where(self.n > 1, (self.s2 - self.n * mean * mean) / np.maximum(self.n - 1, 1), np.nan)
        var = np.where(var < 0, 0.0, var)
        std = np.where(self.n > 1, np.sqrt(var), np.nan)
        return mean, std, self.n.copy()


def main():
    print("=" * 70)
    print("Winners vs losers — descriptive comparison (memory-safe online stats)")
    print("=" * 70)

    with open(os.path.join(CACHE_DIR, "universe_ohlcv_daily.pkl"), "rb") as f:
        universe = pickle.load(f)
    print(f"  OHLCV tickers: {len(universe):,}")

    with sqlite3.connect(DB) as c:
        ex_rows = c.execute(
            "SELECT setup_type, ticker, entry_date FROM examples "
            "WHERE setup_type IN ('htf','bf','base','dtss')"
        ).fetchall()

    examples_by_setup = defaultdict(list)
    for s, t, d in ex_rows:
        examples_by_setup[s].append((t, d))

    expr = ExprSeriesCache()
    feature_names = expr.expr_names
    n_features = len(feature_names)
    print(f"  Features: {n_features:,}")
    adr_col = expr.expr_index("adr14")

    # Phase 1: build clusters per setup (OHLCV + cache dates only)
    all_clusters = []
    ticker_to_clusters = defaultdict(list)
    ticker_to_alignment = {}

    for setup in ACTIVE:
        pyr_path = latest_pyramid(setup)
        if pyr_path is None: continue
        pyr = json.load(open(pyr_path))
        sigs = final_signals_from_pyramid(pyr)
        klass = "fade" if setup in FADE_SETUPS else "breakout"
        print(f"  {setup}: {len(sigs)} raw signals")

        per_ticker_sig_dates = defaultdict(list)
        for s in sigs:
            per_ticker_sig_dates[s["ticker"]].append(s["date"])

        for tk, sdates in per_ticker_sig_dates.items():
            if tk not in ticker_to_alignment:
                df = universe.get(tk)
                if df is None:
                    ticker_to_alignment[tk] = None; continue
                cd, cdata = expr.get_ticker(tk)
                cdata = None; gc.collect()
                if cd is None:
                    ticker_to_alignment[tk] = None; continue
                first_cache_date = str(cd[0])[:10]
                df_a, dates_a = align_ohlcv_len(df, len(cd), first_cache_date)
                if df_a is None:
                    ticker_to_alignment[tk] = None; continue
                ticker_to_alignment[tk] = (df_a, dates_a)
            if ticker_to_alignment[tk] is None: continue
            df_a, dates_a = ticker_to_alignment[tk]
            bar_idxs = []
            for sd in sdates:
                hits = np.where(dates_a == sd)[0]
                if len(hits) == 0: continue
                bar_idxs.append(int(hits[0]))
            bar_idxs = sorted(set(bar_idxs))
            i = 0
            while i < len(bar_idxs):
                j = i + 1
                while j < len(bar_idxs) and bar_idxs[j] == bar_idxs[j-1] + 1:
                    j += 1
                cluster_bars = bar_idxs[i:j]
                c = {
                    "setup": setup, "class": klass, "ticker": tk,
                    "bars": cluster_bars, "leftmost": cluster_bars[0],
                    "rightmost": cluster_bars[-1], "size": len(cluster_bars),
                    "is_winner": False, "entry_idx": None,
                }
                all_clusters.append(c)
                ticker_to_clusters[tk].append(len(all_clusters) - 1)
                i = j

        # Tag winners
        for tk, ed in examples_by_setup[setup]:
            al = ticker_to_alignment.get(tk)
            if al is None: continue
            df_a, dates_a = al
            hits = np.where(dates_a == ed)[0]
            if len(hits) == 0: continue
            entry_idx = int(hits[0])
            for cid in ticker_to_clusters[tk]:
                c = all_clusters[cid]
                if c["setup"] != setup: continue
                if c["leftmost"] <= entry_idx <= c["rightmost"] + 1:
                    c["is_winner"] = True
                    c["entry_idx"] = entry_idx
                    break

        n_w = sum(1 for c in all_clusters if c["setup"] == setup and c["is_winner"])
        n_l = sum(1 for c in all_clusters if c["setup"] == setup and not c["is_winner"])
        print(f"    winners={n_w}  losers={n_l}")

    total_fb = sum(c["size"] for c in all_clusters)
    print(f"\n  total clusters={len(all_clusters)}  firing bars={total_fb}")

    # Phase 2: for each ticker, load cdata once, iterate that ticker's clusters,
    # update running stats. Release cdata.
    stats = {
        "breakout": {"winner": OnlineGroupStats(n_features), "loser": OnlineGroupStats(n_features)},
        "fade":     {"winner": OnlineGroupStats(n_features), "loser": OnlineGroupStats(n_features)},
    }
    shape_rows = {"breakout_winner": [], "breakout_loser": [],
                  "fade_winner": [], "fade_loser": []}

    processed = 0
    for tk, cids in ticker_to_clusters.items():
        if ticker_to_alignment.get(tk) is None: continue
        if not cids: continue
        df_a, _ = ticker_to_alignment[tk]
        cd, cdata = expr.get_ticker(tk)
        if cd is None or cdata is None or len(cdata) != len(df_a):
            cdata = None; gc.collect(); continue
        highs = df_a["high"].values; lows = df_a["low"].values
        opens = df_a["open"].values; closes = df_a["close"].values
        for cid in cids:
            c = all_clusters[cid]
            klass = c["class"]
            key_short = "winner" if c["is_winner"] else "loser"
            shape_key = f"{klass}_{key_short}"
            for bi in c["bars"]:
                if bi >= len(cdata): continue
                vec = cdata[bi]  # view into cdata; don't need to copy for stats
                stats[klass][key_short].update(vec)
                # bar shape
                o = float(opens[bi]); h = float(highs[bi]); l = float(lows[bi]); cl = float(closes[bi])
                rng = h - l
                body_frac = (abs(cl - o) / rng) if rng > 0 else 0.0
                close_pos = ((cl - l) / rng) if rng > 0 else 0.5
                adr = float(cdata[bi, adr_col]) if adr_col is not None else np.nan
                if not np.isfinite(adr) or adr <= 0:
                    h14 = highs[max(0, bi-13):bi+1]
                    l14 = lows[max(0, bi-13):bi+1]
                    adr = float(np.mean(h14 - l14)) if len(h14) else np.nan
                range_adr = (rng / adr) if (adr and adr > 0) else np.nan
                shape_rows[shape_key].append((range_adr, body_frac, close_pos))
        cdata = None; cd = None
        gc.collect()
        processed += 1
        if processed % 500 == 0:
            print(f"    processed {processed} tickers")
    print(f"  processed {processed} tickers")

    # Free large structures
    del ticker_to_alignment, universe
    gc.collect()

    # Report
    print()
    print("=" * 70)
    print("REPORT")
    print("=" * 70)

    for klass in ["breakout", "fade"]:
        winner_clusters = [c for c in all_clusters if c["class"] == klass and c["is_winner"]]
        if not winner_clusters: continue
        fw = np.array([c["entry_idx"] - c["leftmost"] for c in winner_clusters])
        rm = np.array([c["entry_idx"] - c["rightmost"] for c in winner_clusters])
        print(f"\n[{klass}] Forward-window envelope (n={len(winner_clusters)} winner clusters):")
        print(f"  FW (entry_idx - leftmost):     min={fw.min()}  p10={np.percentile(fw,10):.0f}  p50={np.percentile(fw,50):.0f}  p90={np.percentile(fw,90):.0f}  max={fw.max()}")
        print(f"  rightmost offset (entry-rm):   min={rm.min()}  p10={np.percentile(rm,10):.0f}  p50={np.percentile(rm,50):.0f}  p90={np.percentile(rm,90):.0f}  max={rm.max()}")
        u, ct = np.unique(rm, return_counts=True)
        print(f"  rightmost-offset distribution:")
        for uu, cc in zip(u, ct):
            print(f"    offset={int(uu):+d}  n={int(cc)}  ({cc/len(rm)*100:.1f}%)")

    for klass in ["breakout", "fade"]:
        w = [c["size"] for c in all_clusters if c["class"] == klass and c["is_winner"]]
        l = [c["size"] for c in all_clusters if c["class"] == klass and not c["is_winner"]]
        print(f"\n[{klass}] Cluster size:")
        if w:
            w = np.array(w); print(f"  winners (n={len(w)}):  mean={w.mean():.2f}  med={np.median(w):.0f}  p90={np.percentile(w,90):.0f}  max={w.max()}")
        if l:
            l = np.array(l); print(f"  losers  (n={len(l)}):  mean={l.mean():.2f}  med={np.median(l):.0f}  p90={np.percentile(l,90):.0f}  max={l.max()}")

    for klass in ["breakout", "fade"]:
        wsh = np.array(shape_rows[f"{klass}_winner"])
        lsh = np.array(shape_rows[f"{klass}_loser"])
        if len(wsh) == 0 or len(lsh) == 0: continue
        print(f"\n[{klass}] Firing-bar shape (median across firing bars):")
        for i, name in enumerate(["rng_adr", "body_frac", "close_pos"]):
            wcol = wsh[:, i]; lcol = lsh[:, i]
            wcol = wcol[np.isfinite(wcol)]; lcol = lcol[np.isfinite(lcol)]
            wm = np.median(wcol) if len(wcol) else np.nan
            lm = np.median(lcol) if len(lcol) else np.nan
            print(f"  {name:>10}  winner_med={wm:+.3f}  loser_med={lm:+.3f}  diff={wm-lm:+.3f}")

    for klass in ["breakout", "fade"]:
        mW, sW, nW = stats[klass]["winner"].finalize()
        mL, sL, nL = stats[klass]["loser"].finalize()
        print(f"\n[{klass}] Feature separation summary:  winner n_max={int(nW.max())}  loser n_max={int(nL.max())}")
        pooled = np.sqrt((sW**2 + sL**2) / 2.0)
        sep = np.abs(mW - mL) / np.where(pooled > 0, pooled, np.nan)
        df = pd.DataFrame({
            "feature": feature_names,
            "mean_winner": mW, "mean_loser": mL,
            "std_winner": sW, "std_loser": sL,
            "n_winner": nW.astype(int), "n_loser": nL.astype(int),
            "sep_z": sep,
        }).dropna(subset=["sep_z"])
        # Require both groups to have enough finite samples to keep it meaningful
        min_n = 20
        df = df[(df["n_winner"] >= min_n) & (df["n_loser"] >= min_n)]
        df = df.sort_values("sep_z", ascending=False)
        path = os.path.join(OUT_DIR, f"16_winners_vs_losers_{klass}.csv")
        df.to_csv(path, index=False)
        print(f"  wrote {path}  ({len(df)} features ranked with n>={min_n})")
        print(f"  Top 20 by separation (|sep_z|):")
        for _, r in df.head(20).iterrows():
            print(f"    {r['feature'][:50]:<50}  sep_z={r['sep_z']:5.2f}  W={r['mean_winner']:+9.3f}  L={r['mean_loser']:+9.3f}  nW={r['n_winner']}  nL={r['n_loser']}")


if __name__ == "__main__":
    main()
