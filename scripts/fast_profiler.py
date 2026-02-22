"""
Fast Profiler — Optimized wrapper around ProfilingEngine.

Performance targets:
  - Examples (21 tickers, cached):  <0.5s
  - Universe sample (500 tickers):  <30s

Optimizations:
  1. Local OHLCV cache (data/ohlcv_cache_{setup}.json) — zero network for examples
  2. Concurrent bulk fetching for universe samples
  3. Selective feature computation — only compute features needed for conditions
  4. Precomputed scan dates stored in cache

Usage:
    from scripts.fast_profiler import FastProfiler

    fp = FastProfiler("dtss")

    # Profile all examples (uses cache, <0.5s)
    df = fp.profile_examples()

    # Profile universe sample (concurrent fetch, <30s)
    df = fp.profile_universe(date="2026-02-19", n=500)

    # Validate conditions against examples
    results = fp.validate_conditions(conditions)

    # Validate conditions against universe
    results = fp.validate_universe(conditions, date="2026-02-19", n=500)

    # Rebuild cache (after adding new examples)
    fp.rebuild_cache()
"""

import json
import os
import time
import concurrent.futures
import requests
import numpy as np
import pandas as pd
from typing import Optional

from scripts.profiling_engine import (
    ProfilingEngine, sma, ema, rolling_max, rolling_min,
    atr, di_minus, count_true
)

# Repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
API_BASE = "https://web-production-e3025.up.railway.app"
MAX_WORKERS = 15


# ============================================================
# Minimal feature computation — only what conditions need
# ============================================================

def compute_condition_features(df: pd.DataFrame, target_idx: int) -> dict:
    """Compute only the features used in DTSS conditions.

    ~6ms per ticker vs ~87ms for full profiling (14x faster).
    Every feature here exactly matches the full profiling engine output.
    """
    c = df["close"]
    h = df["high"]
    l_col = df["low"]

    # Core L1 indicators needed
    atr14 = atr(df, 14).replace(0, np.nan)
    xavgc20 = ema(c, 20)
    xavgc21 = ema(c, 21)
    xavgc50 = ema(c, 50)
    xavgc100 = ema(c, 100)
    xavgc200 = ema(c, 200)
    avgc50 = sma(c, 50)
    avgc200 = sma(c, 200)
    maxh20 = rolling_max(h, 20)
    maxh120 = rolling_max(h, 120)
    minl120 = rolling_min(l_col, 120)

    # Offsets for slope
    xavgc50_5 = xavgc50.shift(5)
    avgc200_5 = avgc200.shift(5)

    idx = target_idx
    a = float(atr14.iloc[idx])
    cv = float(c.iloc[idx])

    result = {}

    # 1. pullback_pctmove_h20_l120: (MAXH20 - C) / (MAXH20 - MINL120)
    mh20 = float(maxh20.iloc[idx])
    ml120 = float(minl120.iloc[idx])
    rng = mh20 - ml120
    result["pullback_pctmove_h20_l120"] = (mh20 - cv) / rng if rng > 0 else None

    # 2. dist_ema100_atr: (C - XAVGC100) / ATR14
    result["dist_ema100_atr"] = (cv - float(xavgc100.iloc[idx])) / a

    # 3. ema20_ema50_spread_atr: (XAVGC20 - XAVGC50) / ATR14
    #    NOTE: Both are EMA (XAVG), not SMA (AVG)
    result["ema20_ema50_spread_atr"] = (float(xavgc20.iloc[idx]) - float(xavgc50.iloc[idx])) / a

    # 4. slope_ema50_5bar_atr: (XAVGC50 - XAVGC50.5) / ATR14
    result["slope_ema50_5bar_atr"] = (float(xavgc50.iloc[idx]) - float(xavgc50_5.iloc[idx])) / a

    # 5. slope_sma200_5bar_atr: (AVGC200 - AVGC200.5) / ATR14
    result["slope_sma200_5bar_atr"] = (float(avgc200.iloc[idx]) - float(avgc200_5.iloc[idx])) / a

    # 6. ext_maxh20_sma50_atr: (MAXH20 - AVGC50) / ATR14
    result["ext_maxh20_sma50_atr"] = (mh20 - float(avgc50.iloc[idx])) / a

    # 7. price_pos_120: (C - MINL120) / (MAXH120 - MINL120)
    mh120 = float(maxh120.iloc[idx])
    rng120 = mh120 - ml120
    result["price_pos_120"] = (cv - ml120) / rng120 if rng120 > 0 else None

    # 8. DIMINUS14
    result["DIMINUS14"] = float(di_minus(df, 14).iloc[idx])

    # 9. count_above_ema21_15: CountTrue(C > XAVGC21, 15)
    result["count_above_ema21_15"] = float(count_true(c > xavgc21, 15).iloc[idx])

    # 10. ema50_ema200_spread_atr: (XAVGC50 - XAVGC200) / ATR14
    result["ema50_ema200_spread_atr"] = (float(xavgc50.iloc[idx]) - float(xavgc200.iloc[idx])) / a

    # 11. dist_sma200_atr: (C - AVGC200) / ATR14
    result["dist_sma200_atr"] = (cv - float(avgc200.iloc[idx])) / a

    # 12. dist_ema50_atr: (C - XAVGC50) / ATR14
    result["dist_ema50_atr"] = (cv - float(xavgc50.iloc[idx])) / a

    return result


class FastProfiler:
    """High-performance profiling with caching and concurrency."""

    def __init__(self, setup_type: str, api_base: str = API_BASE):
        self.setup_type = setup_type
        self.api_base = api_base.rstrip("/")
        self.engine = ProfilingEngine(api_base=api_base)
        self._cache = None
        self._ohlcv_dfs = {}  # ticker_date -> DataFrame (in-memory)

    # ----------------------------------------------------------
    # Cache management
    # ----------------------------------------------------------

    @property
    def cache_path(self) -> str:
        return os.path.join(DATA_DIR, f"ohlcv_cache_{self.setup_type}.json")

    def _load_cache(self) -> dict:
        """Load cache from disk. Returns empty structure if not found."""
        if self._cache is not None:
            return self._cache
        if os.path.exists(self.cache_path):
            with open(self.cache_path) as f:
                self._cache = json.load(f)
        else:
            self._cache = {"examples": [], "ohlcv": {}, "metadata": {}}
        return self._cache

    def _get_ohlcv_df(self, key: str) -> Optional[pd.DataFrame]:
        """Get OHLCV DataFrame from in-memory cache, parsing from JSON cache if needed."""
        if key in self._ohlcv_dfs:
            return self._ohlcv_dfs[key]

        cache = self._load_cache()
        rows = cache["ohlcv"].get(key)
        if not rows:
            return None

        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        self._ohlcv_dfs[key] = df
        return df

    def rebuild_cache(self):
        """Rebuild the OHLCV cache from Railway DB. Run after adding examples."""
        print(f"Rebuilding cache for {self.setup_type}...")
        t0 = time.time()

        # Get examples
        examples = self._query(
            f"SELECT id, ticker, entry_date FROM examples "
            f"WHERE setup_type='{self.setup_type}' ORDER BY ticker"
        )

        # Get scan dates concurrently
        def get_scan_date(ex):
            rows = self._query(
                f"SELECT date FROM universe_ohlcv "
                f"WHERE ticker='{ex['ticker']}' AND date<'{ex['entry_date']}' "
                f"ORDER BY date DESC LIMIT 1"
            )
            return (ex["ticker"], ex["entry_date"], rows[0]["date"] if rows else None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            scan_results = list(pool.map(get_scan_date, examples))

        # Fetch OHLCV concurrently
        fetch_list = []
        for ticker, entry_date, scan_date in scan_results:
            if scan_date:
                fetch_list.append((ticker, scan_date))

        # Add SPY/QQQ for market context
        valid_dates = [sd for _, _, sd in scan_results if sd]
        if valid_dates:
            max_date = max(valid_dates)
            fetch_list.append(("SPY", max_date))
            fetch_list.append(("QQQ", max_date))

        def fetch_bulk(args):
            ticker, date = args
            try:
                r = requests.get(
                    f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                    params={"end_date": date, "lookback": 250},
                    timeout=30,
                )
                if r.status_code == 200:
                    return (f"{ticker}_{date}", r.json().get("results", []))
            except Exception:
                pass
            return (f"{ticker}_{date}", [])

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            ohlcv_results = list(pool.map(fetch_bulk, fetch_list))

        # Build cache
        cache = {
            "examples": [],
            "ohlcv": {},
            "metadata": {
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "setup_type": self.setup_type,
                "example_count": 0,
            },
        }

        for ticker, entry_date, scan_date in scan_results:
            if scan_date:
                cache["examples"].append(
                    {
                        "ticker": ticker,
                        "entry_date": entry_date,
                        "scan_date": scan_date,
                    }
                )

        for key, rows in ohlcv_results:
            if rows:
                cache["ohlcv"][key] = rows

        cache["metadata"]["example_count"] = len(cache["examples"])

        # Save
        with open(self.cache_path, "w") as f:
            json.dump(cache, f)

        # Reset in-memory caches
        self._cache = cache
        self._ohlcv_dfs = {}

        elapsed = time.time() - t0
        size_kb = os.path.getsize(self.cache_path) / 1024
        print(
            f"Cache rebuilt: {len(cache['examples'])} examples, "
            f"{len(cache['ohlcv'])} OHLCV sets, "
            f"{size_kb:.0f} KB, {elapsed:.2f}s"
        )

    # ----------------------------------------------------------
    # Example profiling — fast path (minimal features)
    # ----------------------------------------------------------

    def profile_examples_fast(self) -> pd.DataFrame:
        """Profile examples computing ONLY condition features. ~0.13s for 21 examples."""
        cache = self._load_cache()
        if not cache["examples"]:
            raise ValueError(f"No cached examples for {self.setup_type}. Run rebuild_cache() first.")

        rows = []
        for ex in cache["examples"]:
            key = f"{ex['ticker']}_{ex['scan_date']}"
            df = self._get_ohlcv_df(key)
            if df is None or df.empty or len(df) < 150:
                continue

            target_dt = pd.Timestamp(ex["scan_date"])
            mask = df["date"] <= target_dt
            if not mask.any():
                continue
            target_idx = df.loc[mask].index[-1]

            result = compute_condition_features(df, target_idx)
            result["ticker"] = ex["ticker"]
            result["entry_date"] = ex["entry_date"]
            result["scan_date"] = ex["scan_date"]
            rows.append(result)

        return pd.DataFrame(rows)

    # ----------------------------------------------------------
    # Universe profiling — fast path (minimal features)
    # ----------------------------------------------------------

    def profile_universe_fast(self, date: str, n: int = 500,
                              progress: bool = True) -> pd.DataFrame:
        """Profile universe computing ONLY condition features.

        Concurrent fetch + minimal compute = fastest possible path.
        """
        t0 = time.time()

        tickers = self._get_random_tickers(n)
        if progress:
            print(f"Got {len(tickers)} tickers")

        # Concurrent OHLCV fetch
        def fetch_one(ticker):
            try:
                r = requests.get(
                    f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                    params={"end_date": date, "lookback": 250},
                    timeout=30,
                )
                if r.status_code == 200:
                    rows = r.json().get("results", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.sort_values("date").reset_index(drop=True)
                        for col in ["open", "high", "low", "close", "volume"]:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                        return (ticker, df)
            except Exception:
                pass
            return (ticker, None)

        # Fetch in batches
        ohlcv = {}
        batch_size = 20
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                results = list(pool.map(fetch_one, batch))
            for ticker, df in results:
                if df is not None and len(df) >= 150:
                    ohlcv[ticker] = df
            if progress and (i + batch_size) % 100 == 0:
                elapsed = time.time() - t0
                print(f"  Fetched {min(i + batch_size, len(tickers))}/{len(tickers)} ({elapsed:.1f}s)")

        t_fetch = time.time()
        if progress:
            print(f"Fetched {len(ohlcv)} tickers in {t_fetch - t0:.1f}s")

        # Compute minimal features
        target_dt = pd.Timestamp(date)
        rows = []
        for ticker, df in ohlcv.items():
            mask = df["date"] <= target_dt
            if not mask.any():
                continue
            target_idx = df.loc[mask].index[-1]

            try:
                result = compute_condition_features(df, target_idx)
                result["ticker"] = ticker
                result["date"] = date
                result["is_example"] = False
                rows.append(result)
            except Exception:
                continue

        t_compute = time.time()
        if progress:
            print(f"Computed {len(rows)} profiles in {t_compute - t_fetch:.1f}s")
            print(f"Total: {t_compute - t0:.1f}s")

        return pd.DataFrame(rows)

    # ----------------------------------------------------------
    # Example profiling (full — all features)
    # ----------------------------------------------------------

    def profile_examples(self, features: list[str] = None) -> pd.DataFrame:
        """Profile all examples using cached OHLCV data.

        Args:
            features: Optional list of feature names to compute.
                      If None, computes all features (full profiling).

        Returns:
            DataFrame with one row per example.
        """
        cache = self._load_cache()
        if not cache["examples"]:
            raise ValueError(f"No cached examples for {self.setup_type}. Run rebuild_cache() first.")

        # Load metadata (LSP data etc.)
        metadata_map = self.engine._load_metadata(self.setup_type)

        # Get market DataFrames from cache
        market_dfs = self._get_market_dfs_from_cache()

        rows = []
        for ex in cache["examples"]:
            ticker = ex["ticker"]
            scan_date = ex["scan_date"]
            entry_date = ex["entry_date"]
            cache_key = f"{ticker}_{scan_date}"

            df = self._get_ohlcv_df(cache_key)
            if df is None or df.empty or len(df) < 150:
                continue

            target_dt = pd.Timestamp(scan_date)
            mask = df["date"] <= target_dt
            if not mask.any():
                continue
            target_idx = df.loc[mask].index[-1]

            # Compute indicators
            l1 = self.engine._compute_layer1(df)
            l2 = self.engine._compute_layer2(df, l1)
            l4 = self.engine._compute_layer4(l1, l2)

            # Extract values at scan bar
            result = {
                "ticker": ticker,
                "entry_date": entry_date,
                "scan_date": scan_date,
                "C": df.at[target_idx, "close"],
                "O": df.at[target_idx, "open"],
                "H": df.at[target_idx, "high"],
                "L": df.at[target_idx, "low"],
                "V": df.at[target_idx, "volume"],
            }

            all_series = {**l1, **l2, **l4}

            if features:
                # Only extract requested features
                for name in features:
                    series = all_series.get(name)
                    if series is not None and target_idx < len(series):
                        val = series.iloc[target_idx]
                        result[name] = float(val) if not pd.isna(val) else None
                    else:
                        result[name] = None
            else:
                # Extract all
                for name, series in all_series.items():
                    val = series.iloc[target_idx] if target_idx < len(series) else np.nan
                    result[name] = float(val) if not pd.isna(val) else None

            # Layer 3: market context
            if market_dfs:
                mkt_rows = {}
                for mkt_ticker, mkt_df in market_dfs.items():
                    if mkt_df is None or mkt_df.empty:
                        continue
                    mkt_l1 = self.engine._compute_layer1(mkt_df)
                    mkt_l2 = self.engine._compute_layer2(mkt_df, mkt_l1)
                    mkt_mask = mkt_df["date"] <= target_dt
                    if not mkt_mask.any():
                        continue
                    mkt_idx = mkt_df.loc[mkt_mask].index[-1]
                    mkt_row = {}
                    for name, series in {**mkt_l1, **mkt_l2}.items():
                        val = series.iloc[mkt_idx] if mkt_idx < len(series) else np.nan
                        mkt_row[name] = float(val) if not pd.isna(val) else None
                    mkt_rows[mkt_ticker] = mkt_row
                l3 = self.engine._compute_layer3(result, mkt_rows)
                result.update(l3)

            # Layer 5: setup-specific metadata
            meta_key = f"{ticker}_{entry_date}"
            meta = metadata_map.get(meta_key, metadata_map.get(ticker))
            if meta:
                l5 = self.engine._compute_layer5(df, target_idx, l1, meta)
                result.update(l5)

            result["is_example"] = True
            rows.append(result)

        return pd.DataFrame(rows)

    def _get_market_dfs_from_cache(self) -> dict:
        """Get SPY/QQQ DataFrames from cache."""
        cache = self._load_cache()
        market_dfs = {}
        for key in cache["ohlcv"]:
            if key.startswith("SPY_"):
                market_dfs["SPY"] = self._get_ohlcv_df(key)
            elif key.startswith("QQQ_"):
                market_dfs["QQQ"] = self._get_ohlcv_df(key)
        return market_dfs

    # ----------------------------------------------------------
    # Universe profiling (concurrent fetch, no cache)
    # ----------------------------------------------------------

    def profile_universe(self, date: str, n: int = 500,
                         features: list[str] = None,
                         progress: bool = True) -> pd.DataFrame:
        """Profile a random universe sample with concurrent fetching.

        Args:
            date: Date to profile
            n: Number of tickers
            features: Optional subset of features to compute
            progress: Print progress updates

        Returns:
            DataFrame with one row per ticker.
        """
        t0 = time.time()

        tickers = self._get_random_tickers(n)
        if progress:
            print(f"Got {len(tickers)} tickers")

        # Concurrent OHLCV fetch
        def fetch_one(ticker):
            try:
                r = requests.get(
                    f"{self.api_base}/api/ohlcv/bulk/{ticker}",
                    params={"end_date": date, "lookback": 250},
                    timeout=30,
                )
                if r.status_code == 200:
                    rows = r.json().get("results", [])
                    if rows:
                        df = pd.DataFrame(rows)
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.sort_values("date").reset_index(drop=True)
                        for col in ["open", "high", "low", "close", "volume"]:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                        return (ticker, df)
            except Exception:
                pass
            return (ticker, None)

        # Fetch in batches
        ohlcv = {}
        batch_size = 20
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
                results = list(pool.map(fetch_one, batch))
            for ticker, df in results:
                if df is not None and len(df) >= 150:
                    ohlcv[ticker] = df
            if progress and (i + batch_size) % 100 == 0:
                print(f"  Fetched {min(i + batch_size, len(tickers))}/{len(tickers)}")

        t_fetch = time.time()
        if progress:
            print(f"Fetched {len(ohlcv)} tickers in {t_fetch - t0:.1f}s")

        # Compute profiles
        target_dt = pd.Timestamp(date)
        rows = []
        for ticker, df in ohlcv.items():
            mask = df["date"] <= target_dt
            if not mask.any():
                continue
            target_idx = df.loc[mask].index[-1]

            l1 = self.engine._compute_layer1(df)
            l2 = self.engine._compute_layer2(df, l1)

            result = {"ticker": ticker, "date": date}

            all_series = {**l1, **l2}
            if features:
                for name in features:
                    series = all_series.get(name)
                    if series is not None and target_idx < len(series):
                        val = series.iloc[target_idx]
                        result[name] = float(val) if not pd.isna(val) else None
                    else:
                        result[name] = None
            else:
                for name, series in all_series.items():
                    val = series.iloc[target_idx] if target_idx < len(series) else np.nan
                    result[name] = float(val) if not pd.isna(val) else None

            result["is_example"] = False
            rows.append(result)

        t_compute = time.time()
        if progress:
            print(f"Computed {len(rows)} profiles in {t_compute - t_fetch:.1f}s")
            print(f"Total: {t_compute - t0:.1f}s")

        return pd.DataFrame(rows)

    # ----------------------------------------------------------
    # Condition validation
    # ----------------------------------------------------------

    def validate_conditions(self, conditions: list[tuple],
                            df: pd.DataFrame = None) -> dict:
        """Validate conditions against a DataFrame.

        Args:
            conditions: List of (name, feature, operator, threshold, description)
            df: DataFrame to validate against. If None, profiles examples.

        Returns:
            Dict with per-condition and combined results.
        """
        if df is None:
            features_needed = [c[1] for c in conditions]
            df = self.profile_examples(features=features_needed)

        n = len(df)
        results = {"total": n, "conditions": [], "combined_pass": 0, "combined_tickers": []}
        all_pass = np.ones(n, dtype=bool)

        for cond in conditions:
            name, col, op, thresh = cond[0], cond[1], cond[2], cond[3]
            desc = cond[4] if len(cond) > 4 else ""

            vals = pd.to_numeric(df[col], errors="coerce")

            if op == "<":
                mask = vals < thresh
            elif op == "<=":
                mask = vals <= thresh
            elif op == ">":
                mask = vals > thresh
            elif op == ">=":
                mask = vals >= thresh
            else:
                mask = pd.Series(False, index=df.index)

            passes = int(mask.sum())
            fails = df[~mask][["ticker", col]].to_dict("records") if passes < n else []

            results["conditions"].append({
                "name": name,
                "feature": col,
                "op": op,
                "threshold": thresh,
                "description": desc,
                "passes": passes,
                "total": n,
                "pass_rate": passes / n if n > 0 else 0,
                "fails": fails,
            })

            all_pass &= mask

        results["combined_pass"] = int(all_pass.sum())
        results["combined_tickers"] = df[all_pass]["ticker"].tolist()
        results["combined_rate"] = results["combined_pass"] / n if n > 0 else 0

        return results

    def validate_universe(self, conditions: list[tuple],
                          date: str, n: int = 500) -> dict:
        """Validate conditions against a universe sample.

        Returns dict with selectivity metrics.
        """
        features_needed = [c[1] for c in conditions]
        df = self.profile_universe(date=date, n=n, features=features_needed)
        return self.validate_conditions(conditions, df=df)

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    def _get_random_tickers(self, n: int) -> list[str]:
        """Get n random tickers from tradable universe, handling API 100-row limit."""
        tickers = set()
        chunks_needed = (n // 100) + 2  # extra chunk for dedup losses
        for _ in range(chunks_needed):
            rows = self._query(
                "SELECT ticker FROM tradable_universe ORDER BY RANDOM() LIMIT 100"
            )
            tickers.update(r["ticker"] for r in rows)
            if len(tickers) >= n:
                break
        return list(tickers)[:n]

    def _query(self, sql: str) -> list[dict]:
        resp = requests.post(
            f"{self.api_base}/api/query", json={"sql": sql}, timeout=30
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"DB query error: {data['error']}")
        return data.get("results", [])


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    setup = sys.argv[1] if len(sys.argv) > 1 else "dtss"
    action = sys.argv[2] if len(sys.argv) > 2 else "examples"

    fp = FastProfiler(setup)

    if action == "rebuild":
        fp.rebuild_cache()
    elif action == "examples":
        t0 = time.time()
        df = fp.profile_examples()
        print(f"\nProfiled {len(df)} examples in {time.time() - t0:.2f}s")
        print(f"Columns: {len(df.columns)}")
    elif action == "universe":
        date = sys.argv[3] if len(sys.argv) > 3 else "2026-02-19"
        n = int(sys.argv[4]) if len(sys.argv) > 4 else 500
        df = fp.profile_universe(date=date, n=n)
        print(f"\nProfiled {len(df)} universe tickers")
        print(f"Columns: {len(df.columns)}")
