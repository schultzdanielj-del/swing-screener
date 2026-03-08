"""
Grind Uploader — Transactional upload of grind results to Railway.

Every grind run (signal_grind or refinement_grind) calls upload() after writing
local files. This module handles the full Railway upload as a transaction:

    1. Validate the result JSON structure
    2. Create cycle with status="uploading"
    3. Upload conditions
    4. Verify conditions via read-back
    5. Activate cycle as current
    6. Mark status="complete" + store source hash

If any step fails: retry 3x, then write to pending_uploads/ for later retry.
If the defensive logic itself fails, it logs warnings but never blocks the grind.

Usage:
    from local_runner.grind_uploader import upload

    upload(
        result=result_dict,              # pyramid_grinder output dict
        result_path="cache/pyramid_...", # path to local JSON (for hashing)
        step_type="signal_grind",        # or "refinement_grind"
        setup_type="dtss",
        activate=True,
    )

    # On agent startup or next grind, retry any pending uploads:
    from local_runner.grind_uploader import retry_pending
    retry_pending()
"""

import os
import sys
import json
import time
import hashlib
import requests
from datetime import datetime, timezone

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
PENDING_DIR = os.path.join(LOCAL_DIR, "pending_uploads")
API_BASE = "https://web-production-e3025.up.railway.app"

MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]  # seconds between retries


# ════════════════════════════════════════════════════════════════
# Validation (#3 defense — schema mismatch)
# ════════════════════════════════════════════════════════════════

REQUIRED_TOP_KEYS = {"setup_type", "all_conditions", "n_conditions", "timestamp"}
REQUIRED_CONDITION_KEYS = {"tier", "expression_name", "low", "high"}


def validate_result(result):
    """Validate grind result structure before any upload attempt.

    Returns (ok: bool, error_msg: str or None).
    Never raises — always returns a result.
    """
    try:
        if not isinstance(result, dict):
            return False, f"Result is {type(result).__name__}, expected dict"

        missing = REQUIRED_TOP_KEYS - set(result.keys())
        if missing:
            return False, f"Missing top-level keys: {missing}"

        conditions = result["all_conditions"]
        if not isinstance(conditions, list):
            return False, f"all_conditions is {type(conditions).__name__}, expected list"

        if len(conditions) == 0:
            return False, "all_conditions is empty"

        for i, c in enumerate(conditions):
            if not isinstance(c, dict):
                return False, f"Condition {i} is {type(c).__name__}, expected dict"
            missing_c = REQUIRED_CONDITION_KEYS - set(c.keys())
            if missing_c:
                return False, f"Condition {i} missing keys: {missing_c}"

        return True, None

    except Exception as e:
        return False, f"Validation error: {e}"


# ════════════════════════════════════════════════════════════════
# Hashing (#5 defense — manual edit detection)
# ════════════════════════════════════════════════════════════════

def compute_hash(filepath):
    """SHA-256 of a local file. Returns hex string or None on error."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"  [uploader] WARNING: hash computation failed: {e}")
        return None


# ════════════════════════════════════════════════════════════════
# HTTP helpers with retry (#1/#2 defense — crash/network/deploy)
# ════════════════════════════════════════════════════════════════

def _post(path, data, retries=MAX_RETRIES):
    """POST to Railway with retry. Returns (response_json, error_msg)."""
    for attempt in range(retries):
        try:
            r = requests.post(f"{API_BASE}{path}", json=data, timeout=30)
            if r.status_code == 200:
                return r.json(), None
            else:
                err = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            err = f"Network error: {e}"
        except Exception as e:
            err = f"Unexpected error: {e}"

        if attempt < retries - 1:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            print(f"  [uploader] Retry {attempt + 1}/{retries} in {wait}s... ({err})")
            time.sleep(wait)

    return None, err


def _get(path, retries=MAX_RETRIES):
    """GET from Railway with retry. Returns (response_json, error_msg)."""
    for attempt in range(retries):
        try:
            r = requests.get(f"{API_BASE}{path}", timeout=30)
            if r.status_code == 200:
                return r.json(), None
            else:
                err = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            err = f"Network error: {e}"
        except Exception as e:
            err = f"Unexpected error: {e}"

        if attempt < retries - 1:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            time.sleep(wait)

    return None, err


def _patch(path, data, retries=MAX_RETRIES):
    """PATCH to Railway with retry. Returns (response_json, error_msg)."""
    for attempt in range(retries):
        try:
            r = requests.patch(f"{API_BASE}{path}", json=data, timeout=30)
            if r.status_code == 200:
                return r.json(), None
            else:
                err = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            err = f"Network error: {e}"
        except Exception as e:
            err = f"Unexpected error: {e}"

        if attempt < retries - 1:
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            time.sleep(wait)

    return None, err


# ════════════════════════════════════════════════════════════════
# Pending uploads (#1/#2 defense — persistent retry queue)
# ════════════════════════════════════════════════════════════════

def _save_pending(cycle_id, pending_data):
    """Write a pending upload file for later retry."""
    try:
        os.makedirs(PENDING_DIR, exist_ok=True)
        path = os.path.join(PENDING_DIR, f"{cycle_id}.json")
        with open(path, "w") as f:
            json.dump(pending_data, f, indent=2)
        print(f"  [uploader] Saved pending upload: {path}")
        print(f"  [uploader] Will retry on next grind run or `retry_pending()` call.")
    except Exception as e:
        print(f"  [uploader] WARNING: Could not save pending upload: {e}")
        print(f"  [uploader] Local grind file still exists. Manual upload needed.")


def retry_pending():
    """Retry all pending uploads. Call on agent startup or next grind."""
    if not os.path.isdir(PENDING_DIR):
        return 0

    files = [f for f in os.listdir(PENDING_DIR) if f.endswith(".json")]
    if not files:
        return 0

    print(f"\n  [uploader] Found {len(files)} pending upload(s). Retrying...")
    succeeded = 0

    for fname in files:
        path = os.path.join(PENDING_DIR, fname)
        try:
            with open(path) as f:
                pending = json.load(f)

            ok = _execute_upload(
                result=pending["result"],
                cycle_id=pending["cycle_id"],
                step_type=pending["step_type"],
                setup_type=pending["setup_type"],
                source_hash=pending.get("source_hash"),
                activate=pending.get("activate", True),
                grind_params=pending.get("grind_params"),
            )

            if ok:
                os.remove(path)
                succeeded += 1
                print(f"  [uploader] ✓ Pending upload {fname} succeeded, file removed.")
            else:
                print(f"  [uploader] ✗ Pending upload {fname} still failing. Will retry later.")

        except Exception as e:
            print(f"  [uploader] WARNING: Error processing {fname}: {e}")

    return succeeded


# ════════════════════════════════════════════════════════════════
# Core upload transaction (#4 defense — partial upload)
# ════════════════════════════════════════════════════════════════

def _execute_upload(result, cycle_id, step_type, setup_type, source_hash,
                    activate, grind_params):
    """Execute the full upload transaction. Returns True on success.

    Transaction steps:
        1. Create cycle with status="uploading"
        2. Upload conditions
        3. Read back conditions and verify count
        4. If activate: set as current
        5. PATCH status → "complete" + source_hash

    If any step fails, the cycle stays in "uploading" status (visibly broken).
    """
    conditions = result["all_conditions"]
    n_conditions = len(conditions)
    now_str = datetime.now(timezone.utc).isoformat()

    # ── Step 1: Create cycle ──
    print(f"  [uploader] Creating cycle {cycle_id}...")
    cycle_data = {
        "cycle_id": cycle_id,
        "setup_type": setup_type,
        "status": "uploading",
        "n_examples_at_grind": result.get("examples_passing"),
        "created_at": result.get("timestamp", now_str),
        "completed_at": now_str,
        "step_type": step_type,
        "grind_params": json.dumps(grind_params) if grind_params else None,
        "source_hash": source_hash,
    }
    resp, err = _post("/api/v2/cycles", cycle_data)
    if err:
        print(f"  [uploader] ✗ Failed to create cycle: {err}")
        return False

    # If cycle already exists (retry of pending), that's OK — continue
    if resp and resp.get("already_exists"):
        print(f"  [uploader] Cycle {cycle_id} already exists, re-uploading conditions...")

    # ── Step 2: Upload conditions ──
    print(f"  [uploader] Uploading {n_conditions} conditions...")
    cond_payload = {
        "conditions": [
            {
                "tier": c.get("tier", "D1"),
                "expression_name": c.get("expression_name", c.get("expr", "")),
                "low": c.get("low"),
                "high": c.get("high"),
                "filter_power": c.get("filter_power"),
                "sort_order": i,
            }
            for i, c in enumerate(conditions)
        ]
    }
    resp, err = _post(f"/api/v2/cycles/{cycle_id}/conditions", cond_payload)
    if err:
        print(f"  [uploader] ✗ Failed to upload conditions: {err}")
        _mark_error(cycle_id, f"Conditions upload failed: {err}")
        return False

    # ── Step 3: Verify read-back ──
    print(f"  [uploader] Verifying upload...")
    resp, err = _get(f"/api/v2/cycles/{cycle_id}/conditions")
    if err:
        print(f"  [uploader] WARNING: Verification read-back failed: {err}")
        print(f"  [uploader] Proceeding anyway — conditions were POSTed successfully.")
    else:
        uploaded_count = len(resp.get("conditions", []))
        if uploaded_count != n_conditions:
            msg = f"Verification mismatch: sent {n_conditions}, Railway has {uploaded_count}"
            print(f"  [uploader] ✗ {msg}")
            _mark_error(cycle_id, msg)
            return False
        print(f"  [uploader] ✓ Verified: {uploaded_count} conditions in Railway.")

    # ── Step 4: Activate ──
    if activate:
        print(f"  [uploader] Activating cycle as current...")
        resp, err = _post(f"/api/v2/cycles/{cycle_id}/activate", {})
        if err:
            print(f"  [uploader] WARNING: Activation failed: {err}")
            print(f"  [uploader] Conditions are uploaded. Activate manually if needed.")
            # Don't fail the whole upload for this — data is safe

    # ── Step 5: Mark complete ──
    patch_data = {
        "status": "complete",
        "source_hash": source_hash,
        "step_type": step_type,
        "grind_params": json.dumps(grind_params) if grind_params else None,
    }
    resp, err = _patch(f"/api/v2/cycles/{cycle_id}", patch_data)
    if err:
        print(f"  [uploader] WARNING: Status update to 'complete' failed: {err}")
        print(f"  [uploader] Conditions are uploaded and active. Status stuck at 'uploading'.")
        # Don't fail — the data is there, status is cosmetic

    print(f"  [uploader] ✓ Upload complete: {cycle_id}")
    return True


def _mark_error(cycle_id, error_msg):
    """Best-effort: mark a cycle as error status in Railway."""
    try:
        _patch(f"/api/v2/cycles/{cycle_id}", {
            "status": "error",
            "error_msg": error_msg,
        })
    except Exception:
        pass  # Best effort — don't compound failures


# ════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════

def upload(result, result_path, step_type, setup_type, activate=True):
    """Upload a grind result to Railway. The main entry point.

    Args:
        result: dict — the pyramid_grinder output dict
        result_path: str — path to the local JSON file (for hashing)
        step_type: str — "signal_grind" or "refinement_grind"
        setup_type: str — e.g. "dtss"
        activate: bool — if True, set this cycle as current for the setup

    Returns:
        cycle_id: str or None — the cycle ID if upload succeeded, None if failed

    This function NEVER raises. It logs errors and saves pending uploads.
    The grind is never blocked by upload failure.
    """
    print(f"\n{'─'*60}")
    print(f"  GRIND UPLOAD — {step_type} / {setup_type}")
    print(f"{'─'*60}")

    # ── Retry any pending uploads first ──
    retry_pending()

    # ── Validate ──
    ok, err = validate_result(result)
    if not ok:
        print(f"  [uploader] ✗ VALIDATION FAILED: {err}")
        print(f"  [uploader] Upload skipped. Local file preserved at: {result_path}")
        print(f"  [uploader] Fix the schema issue and re-run, or upload manually.")
        return None

    # ── Compute hash ──
    source_hash = compute_hash(result_path)

    # ── Generate cycle ID ──
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    cycle_id = f"{setup_type}_{step_type}_{ts}"

    # ── Build grind params for audit trail ──
    grind_params = result.get("params")

    # ── Attempt upload ──
    ok = _execute_upload(
        result=result,
        cycle_id=cycle_id,
        step_type=step_type,
        setup_type=setup_type,
        source_hash=source_hash,
        activate=activate,
        grind_params=grind_params,
    )

    if ok:
        print(f"  [uploader] ✓ Railway cycle: {cycle_id}")
        print(f"  [uploader] ✓ Local file:    {result_path}")
        return cycle_id
    else:
        # Save pending for retry
        print(f"  [uploader] ✗ Upload failed. Saving for retry...")
        _save_pending(cycle_id, {
            "cycle_id": cycle_id,
            "step_type": step_type,
            "setup_type": setup_type,
            "source_hash": source_hash,
            "activate": activate,
            "grind_params": grind_params,
            "result": result,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        })
        return None
