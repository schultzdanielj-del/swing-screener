"""
grind_upload.py — Upload a pyramid grinder result to Railway as a versioned cycle.

Usage:
    python scripts/grind_upload.py --setup dtss
    python scripts/grind_upload.py --setup dtss --blackout
    python scripts/grind_upload.py --setup dtss --file /path/to/result.json

What it does:
    1. Reads the grinder JSON from cache/ (or --file)
    2. Validates 100% example pass (if field present — hard stop if not)
    3. Creates a grind_cycles row on Railway (status=complete)
    4. Bulk-inserts all cycle_conditions rows
    5. Activates the cycle as current (is_current=1, all others → 0)

Output:
    Prints cycle_id, N conditions uploaded, activated confirmation.
"""

import argparse
import json
import os
import sys
from datetime import timezone
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────

RAILWAY_URL = "https://web-production-e3025.up.railway.app"
REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR   = os.path.join(REPO_ROOT, "cache")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_result(setup_type: str, blackout: bool, file_path: str | None) -> dict:
    """Load grinder JSON from explicit path or default cache location."""
    if file_path:
        path = file_path
    elif blackout:
        path = os.path.join(CACHE_DIR, f"pyramid_results_{setup_type}_blackout.json")
    else:
        path = os.path.join(CACHE_DIR, f"pyramid_results_{setup_type}.json")

    if not os.path.exists(path):
        print(f"ERROR: Result file not found: {path}")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    print(f"  Loaded: {path}")
    return data


def validate_examples(result: dict) -> int:
    """
    Hard-stop if examples_passing field exists and is not 100%.
    Returns examples_passing count (0 if field absent — older format).
    """
    passing = result.get("examples_passing")
    failing = result.get("examples_failing", [])

    if passing is None:
        # Older grind format — no validation field, warn and continue
        print("  WARNING: examples_passing field not present in result. "
              "Older grind format — skipping example validation.")
        return 0

    if failing:
        print(f"HARD STOP: {len(failing)} example(s) failed — this result is invalid.")
        print(f"  Failing: {failing}")
        print("  Re-run the grinder and ensure 100% pass before uploading.")
        sys.exit(1)

    print(f"  Examples passing: {passing} / {passing}  ✓")
    return passing


def derive_cycle_id(setup_type: str, result: dict) -> str:
    """
    Derive cycle_id from grinder timestamp.
    Format: "{setup_type}_{YYYYMMDD}_{HHMMSS}"
    Falls back to current UTC time if timestamp missing.
    """
    ts_raw = result.get("timestamp")
    if ts_raw:
        try:
            # Handles both "2026-03-06T14:30:22.123456+00:00" and "2026-03-06T14:30:22Z"
            ts_raw_clean = ts_raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_raw_clean).astimezone(timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)

    return f"{setup_type}_{dt.strftime('%Y%m%d_%H%M%S')}"


def post(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    """POST to Railway API, raise on non-2xx."""
    url = f"{RAILWAY_URL}{endpoint}"
    r = requests.post(url, json=payload, timeout=timeout)
    if not r.ok:
        print(f"ERROR {r.status_code} — {endpoint}")
        print(f"  Body: {r.text[:400]}")
        sys.exit(1)
    return r.json()


def get(endpoint: str, timeout: int = 15) -> dict:
    """GET from Railway API."""
    url = f"{RAILWAY_URL}{endpoint}"
    r = requests.get(url, timeout=timeout)
    if not r.ok:
        print(f"ERROR {r.status_code} — {endpoint}")
        print(f"  Body: {r.text[:400]}")
        sys.exit(1)
    return r.json()


# ── Main ──────────────────────────────────────────────────────────────────────

def upload(setup_type: str, blackout: bool, file_path: str | None) -> None:
    print(f"\n{'='*60}")
    print(f"  grind_upload — {setup_type.upper()}")
    print(f"{'='*60}\n")

    # 1. Load result
    result = load_result(setup_type, blackout, file_path)

    setup_in_result = result.get("setup_type", setup_type)
    if setup_in_result != setup_type:
        print(f"  WARNING: --setup {setup_type!r} but result has setup_type={setup_in_result!r}. "
              "Using value from result.")
        setup_type = setup_in_result

    # 2. Validate examples
    n_examples = validate_examples(result)

    # 3. Derive cycle_id
    cycle_id = derive_cycle_id(setup_type, result)
    print(f"  cycle_id: {cycle_id}")

    # 4. Build conditions list
    conditions = result.get("all_conditions", [])
    if not conditions:
        print("ERROR: No conditions found in result (all_conditions is empty).")
        sys.exit(1)
    print(f"  Conditions to upload: {len(conditions)}")

    # Normalise each condition to match cycle_conditions schema
    normalised = []
    for i, c in enumerate(conditions):
        normalised.append({
            "tier":            c.get("tier", "D1"),
            "expression_name": c.get("name") or c.get("expr", ""),
            "low":             c.get("low"),
            "high":            c.get("high"),
            "filter_power":    c.get("filter_power"),   # None for older grinds
            "sort_order":      i,
        })

    # Timestamps
    grind_ts  = result.get("timestamp") or datetime.now(timezone.utc).isoformat()
    completed_at = datetime.now(timezone.utc).isoformat()

    # Summary for reporting
    summary = result.get("summary", {})
    n_signals  = summary.get("final_total")
    peak       = summary.get("final_peak")
    avg        = summary.get("final_avg")

    # 5. Create grind_cycles row
    print(f"\n  [1/3] Creating cycle record...")
    cycle_payload = {
        "cycle_id":             cycle_id,
        "setup_type":           setup_type,
        "status":               "complete",
        "n_examples_at_grind":  n_examples or None,
        "created_at":           grind_ts,
        "completed_at":         completed_at,
    }
    resp = post("/api/v2/cycles", cycle_payload)
    if resp.get("already_exists"):
        print(f"  Cycle {cycle_id} already exists on Railway — skipping creation.")
    else:
        print(f"  Created: {cycle_id}")

    # 6. Upload conditions
    print(f"  [2/3] Uploading {len(normalised)} conditions...")
    cond_payload = {
        "cycle_id":   cycle_id,
        "conditions": normalised,
    }
    resp = post(f"/api/v2/cycles/{cycle_id}/conditions", cond_payload, timeout=60)
    n_inserted = resp.get("inserted", len(normalised))
    print(f"  Inserted: {n_inserted} condition rows")

    # 7. Activate as current
    print(f"  [3/3] Activating cycle as current...")
    resp = post(f"/api/v2/cycles/{cycle_id}/activate", {})
    print(f"  Activated: {resp.get('message', 'OK')}")

    # 8. Summary
    print(f"\n{'='*60}")
    print(f"  UPLOAD COMPLETE")
    print(f"  cycle_id   : {cycle_id}")
    print(f"  conditions : {len(normalised)}")
    if n_signals is not None:
        print(f"  signals    : {n_signals} (peak {peak}/day, avg {avg:.1f}/day)")
    print(f"  status     : current  ✓")
    print(f"{'='*60}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Upload pyramid grinder result to Railway as a versioned cycle."
    )
    parser.add_argument("--setup",    required=True, help="Setup type, e.g. dtss")
    parser.add_argument("--blackout", action="store_true",
                        help="Load pyramid_results_{setup}_blackout.json instead of base")
    parser.add_argument("--file",     default=None,
                        help="Explicit path to grinder JSON (overrides --blackout)")
    args = parser.parse_args()

    upload(
        setup_type=args.setup.lower(),
        blackout=args.blackout,
        file_path=args.file,
    )
