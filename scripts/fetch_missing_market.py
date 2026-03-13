"""Fetch only missing instruments and merge into existing market_ohlcv.pkl."""
import os, sys, pickle, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local_runner.market_cache_builder import (
    all_instruments, _fetch_one, OHLCV_PATH, CACHE_DIR
)
from concurrent.futures import ThreadPoolExecutor, as_completed

if __name__ == "__main__":
    # Load existing
    with open(OHLCV_PATH, "rb") as f:
        cache = pickle.load(f)
    print(f"Existing pickle: {len(cache)} instruments")

    # Find missing
    all_inst = all_instruments()
    stooq = [i for i in all_inst if i.startswith("$")]
    missing = [i for i in all_inst if i not in cache and i not in stooq]
    print(f"Missing (non-Stooq): {len(missing)}")
    if not missing:
        print("Nothing to fetch.")
        sys.exit(0)

    for m in missing:
        print(f"  {m}")

    # Fetch missing only
    print(f"\nFetching {len(missing)} instruments...")
    fetched = 0
    failed = []
    with ThreadPoolExecutor(max_workers=4) as pool:  # fewer threads to avoid rate limit
        futures = {pool.submit(_fetch_one, inst): inst for inst in missing}
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

    # Save
    with open(OHLCV_PATH, "wb") as f:
        pickle.dump(cache, f, protocol=4)

    print(f"\nDone. Fetched: {fetched}, Failed: {len(failed)}, Total in pickle: {len(cache)}")
    if failed:
        print(f"Still missing: {failed}")
