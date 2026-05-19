"""
Build worktree-local market_series .npz files for SPY + QQQ against the
extended OHLCV pickle.

Mirrors main repo's market_cache_builder.py compute logic exactly --
imports its expression library, worker initializer, and per-instrument
compute function so the resulting .npz files are byte-schema-identical
to the main repo's market_series/SPY.npz and QQQ.npz, just covering more
history (SPY back to 1993-01-29, QQQ back to 1999-03-10).

Reads:
    regime_meter/cache/market_ohlcv_extended.pkl

Writes:
    regime_meter/cache/market_series/SPY.npz
    regime_meter/cache/market_series/QQQ.npz
    regime_meter/cache/market_series/_manifest.json

All writes guarded to the worktree. Main repo's market_series/ is NOT
modified.
"""
import argparse
import json
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone

import numpy as np

# Make `local_runner.*` and `scripts.*` importable from the main repo.
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)
MAIN_REPO_ROOT = r"C:\Users\Dan\Documents\ScanPerfect\swing-screener"
if MAIN_REPO_ROOT not in sys.path:
    sys.path.insert(0, MAIN_REPO_ROOT)

from local_runner.market_cache_builder import (
    PRICE_ONLY,
    _compute_one,
    _expr_fingerprint,
    _init_compute_worker,
    _load_expressions,
    instrument_filename,
)


# --- Paths -----------------------------------------------------------------
CACHE_DIR     = os.path.join(SCRIPT_DIR, "cache")
EXTENDED_PKL  = os.path.join(CACHE_DIR, "market_ohlcv_extended.pkl")
MKT_DIR       = os.path.join(CACHE_DIR, "market_series")
MANIFEST_PATH = os.path.join(MKT_DIR, "_manifest.json")

INSTRUMENTS = ["SPY", "QQQ"]


def _assert_inside_worktree(path):
    resolved = os.path.abspath(path)
    boundary = (WORKTREE_ROOT + os.sep).lower()
    if not resolved.lower().startswith(boundary):
        sys.exit(
            f"ABORT: path {resolved!r} resolves outside worktree {WORKTREE_ROOT!r}"
        )


def main():
    parser = argparse.ArgumentParser(
        description="Build worktree-local SPY+QQQ market_series npz "
                    "against extended OHLCV"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Recompute even if cache is fresh against the current "
             "expression fingerprint",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("  EXTENDED MARKET SERIES BUILD (regime-meter worktree)")
    print("=" * 70)

    _assert_inside_worktree(EXTENDED_PKL)
    _assert_inside_worktree(MKT_DIR)
    _assert_inside_worktree(MANIFEST_PATH)

    print(f"  Worktree root:    {WORKTREE_ROOT}")
    print(f"  Extended pickle:  {EXTENDED_PKL}")
    print(f"  Output dir:       {MKT_DIR}")
    print(f"  Instruments:      {INSTRUMENTS}")

    if not os.path.exists(EXTENDED_PKL):
        sys.exit(
            f"ABORT: extended pickle missing at {EXTENDED_PKL} "
            "(run regime_meter/extended_ohlcv_ingest.py first)"
        )

    os.makedirs(MKT_DIR, exist_ok=True)

    print(f"\n  Loading extended OHLCV pickle ...")
    with open(EXTENDED_PKL, "rb") as f:
        ohlcv = pickle.load(f)
    print(f"  {len(ohlcv)} instruments in extended pickle")

    missing = [k for k in INSTRUMENTS if k not in ohlcv]
    if missing:
        sys.exit(f"ABORT: extended pickle missing instruments: {missing}")

    print(f"\n  Loading expression library ...")
    expressions = _load_expressions()
    fingerprint = _expr_fingerprint(expressions)
    print(f"  {len(expressions)} expressions (fingerprint: {fingerprint})")

    # Freshness check
    if not args.force and os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            existing = json.load(f)
        if (existing.get("fingerprint") == fingerprint
                and set(existing.get("instruments", {}).keys()) >= set(INSTRUMENTS)):
            print(f"\n  Cache fresh against current expression library. "
                  "Use --force to recompute.")
            for k, info in existing["instruments"].items():
                print(f"    {k:6s} -> {info.get('n_bars'):>6d} bars  "
                      f"last={info.get('last_date')}  "
                      f"n_exprs_valid={info.get('n_exprs_valid')}")
            return

    # Build work items
    work_items = []
    for k in INSTRUMENTS:
        df = ohlcv[k]
        df_dict = {
            "date":   df["date"].values,
            "open":   df["open"].values,
            "high":   df["high"].values,
            "low":    df["low"].values,
            "close":  df["close"].values,
            "volume": df["volume"].values,
        }
        work_items.append((k, df_dict, k in PRICE_ONLY))
        print(f"    {k:6s}: {len(df)} bars "
              f"({df['date'].iloc[0].date()} -> {df['date'].iloc[-1].date()})")

    n_workers = min(len(INSTRUMENTS), 4)
    print(f"\n  Computing {len(work_items)} instruments x {len(expressions)} "
          f"expressions with {n_workers} workers ...")

    t0 = time.time()
    instrument_info = {}
    failed = []

    with ProcessPoolExecutor(
        max_workers=n_workers,
        initializer=_init_compute_worker,
        initargs=(expressions,),
    ) as pool:
        futures = {pool.submit(_compute_one, item): item[0] for item in work_items}
        for fut in as_completed(futures):
            inst_id = futures[fut]
            try:
                _out_id, dates, data = fut.result()
            except Exception as e:
                failed.append((inst_id, repr(e)))
                print(f"    FAIL {inst_id}: {e!r}")
                continue
            if dates is None or data is None:
                failed.append((inst_id, "compute returned None"))
                print(f"    FAIL {inst_id}: returned None")
                continue
            out_path = os.path.join(MKT_DIR, instrument_filename(inst_id))
            _assert_inside_worktree(out_path)
            np.savez_compressed(out_path, data=data, dates=dates)
            n_valid_last = int(np.sum(~np.isnan(data[-1])))
            instrument_info[inst_id] = {
                "n_bars":        len(dates),
                "first_date":    str(dates[0]),
                "last_date":     str(dates[-1]),
                "n_exprs_valid": n_valid_last,
                "price_only":    inst_id in PRICE_ONLY,
            }
            size_mb = os.path.getsize(out_path) / 1024**2
            print(f"    OK   {inst_id:6s} -> {os.path.basename(out_path):20s} "
                  f"{len(dates):>6d} bars  "
                  f"({dates[0]} -> {dates[-1]})  "
                  f"{n_valid_last:>5d}/{len(expressions)} exprs valid at last bar  "
                  f"{size_mb:.1f} MB")

    elapsed = time.time() - t0

    if failed:
        print(f"\n  FAILED: {len(failed)}")
        for inst_id, err in failed:
            print(f"    {inst_id}: {err}")
        sys.exit("ABORT: at least one instrument failed; not writing manifest.")

    manifest = {
        "fingerprint":   fingerprint,
        "n_expressions": len(expressions),
        "expr_names":    [e["name"] for e in expressions],
        "n_instruments": len(instrument_info),
        "instruments":   instrument_info,
        "built_at":      datetime.now(timezone.utc).isoformat(),
        "build_time_s":  round(elapsed, 1),
        "source_pickle": EXTENDED_PKL,
    }
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n  Wrote manifest: {MANIFEST_PATH}")

    total_bytes = sum(
        os.path.getsize(os.path.join(MKT_DIR, f))
        for f in os.listdir(MKT_DIR) if f.endswith(".npz")
    )
    print(f"  Disk:           {total_bytes / 1024**2:.1f} MB")
    print(f"  Time:           {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print("\n  DONE.")


if __name__ == "__main__":
    main()
