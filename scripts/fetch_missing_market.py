"""Fetch only missing instruments and merge into existing market_ohlcv.pkl."""
import os, sys, pickle, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_runner.market_cache_builder import (
    all_instruments, _fetch_one, _is_pickle_instrument, _read_from_daily_pickle,
    _compute_derived_instruments, _standard_df,
    OHLCV_PATH, DAILY_PKL, CACHE_DIR, DERIVED_INSTRUMENTS
)
from concurrent.futures import ThreadPoolExecutor, as_completed

if __name__ == "__main__":
    # Load existing
    with open(OHLCV_PATH, "rb") as f:
        cache = pickle.load(f)
    print(f"Existing pickle: {len(cache)} instruments")

    # Load daily pickle for ETF/stock reads
    daily_cache = None
    if os.path.exists(DAILY_PKL):
        with open(DAILY_PKL, "rb") as f:
            daily_cache = pickle.load(f)
        print(f"Daily pickle: {len(daily_cache)} tickers")
    else:
        print("WARNING: daily pickle not found — pickle instruments will fail")

    # Find missing (skip derived — those are computed, not fetched)
    all_inst = all_instruments()
    missing = [i for i in all_inst
               if i not in cache and i not in DERIVED_INSTRUMENTS]
    print(f"Missing: {len(missing)}")
    if not missing:
        print("Nothing to fetch.")
        sys.exit(0)

    for m in missing:
        print(f"  {m}")

    # Fetch missing — pickle instruments read locally, rest go to network
    print(f"\nFetching {len(missing)} instruments...")
    fetched = 0
    failed = []

    # Pickle instruments (instant, no threading)
    pickle_missing = [i for i in missing if _is_pickle_instrument(i)]
    network_missing = [i for i in missing if not _is_pickle_instrument(i)]

    for inst_id in pickle_missing:
        df = _read_from_daily_pickle(inst_id, daily_cache)
        if df is not None:
            cache[inst_id] = df
            fetched += 1
            print(f"  OK   {inst_id:25s} {len(df)} bars  "
                  f"({df['date'].iloc[0].date()} – {df['date'].iloc[-1].date()})  [pickle]")
        else:
            failed.append(inst_id)
            print(f"  FAIL {inst_id}  [not in daily pickle]")

    # Network instruments (threaded)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_one, inst): inst for inst in network_missing}
        for future in as_completed(futures):
            inst_id, df = future.result()
            if df is not None:
                cache[inst_id] = df
                fetched += 1
                print(f"  OK   {inst_id:25s} {len(df)} bars  "
                      f"({df['date'].iloc[0].date()} – {df['date'].iloc[-1].date()})")
            else:
                failed.append(inst_id)
                print(f"  FAIL {inst_id}")

    # Recompute derived instruments if components are now present
    n_derived = _compute_derived_instruments(cache)
    if n_derived:
        fetched += n_derived
        print(f"\n  Computed {n_derived} derived instruments")

    # Save
    with open(OHLCV_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=4)

    print(f"\nDone. Fetched: {fetched}, Failed: {len(failed)}, Total in pickle: {len(cache)}")
    if failed:
        print(f"Still missing: {failed}")
