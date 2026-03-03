#!/usr/bin/env python3
"""
AI Sample Review — Uses Claude CLI to vet pending examples.

For each pending example:
1. Downloads chart PNG from Railway API
2. Sends to Claude CLI with setup criteria
3. Approve → moves to examples table
4. Reject → moves to rejected table

Usage: python scripts/review_samples.py --setup dtss
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

import requests

API_BASE = os.environ.get("API_BASE", "https://web-production-e3025.up.railway.app")

# Setup-specific review prompts
REVIEW_PROMPTS = {
    "dtss": """You are reviewing a stock chart to determine if it shows a valid DTSS (Double Top Short Sell) setup.

DTSS criteria:
- Clear prior high / resistance level (left side pivot)
- Second rally into the same zone — can be slightly above or below the prior peak
- Rejection candle or reversal pattern at the double top level
- Volume often spikes on the failed attempt then dries up
- MAs may be flattening or starting to roll over
- The stock should be FAILING at or near the double top, not still rallying up to it
- After the double top, price should break down through the LSP (left side pivot) AVWAP and continue lower
- This is a SHORT setup — the stock goes DOWN after entry

The entry bar is marked on the chart. Look at the price action BEFORE the entry to confirm the double top pattern exists, and AFTER to confirm the stock actually broke down.

REJECT if:
- No clear double top pattern visible
- Stock is still in an uptrend with no reversal
- The "double top" is really just consolidation in a trend
- The move after entry is tiny or the stock bounces back up quickly
- Entry is too late (stock already crashed before the marked entry)
- Entry is too early (stock hasn't confirmed the top yet)

Respond with ONLY one word: APPROVE or REJECT""",
}


def get_pending(setup_type):
    """Fetch pending examples from Railway."""
    r = requests.get(f"{API_BASE}/api/pending/{setup_type}", timeout=30)
    r.raise_for_status()
    return r.json().get("pending", [])


def download_chart(setup_type, ticker, entry_date, out_path):
    """Download chart PNG from Railway."""
    url = f"{API_BASE}/api/chart/{setup_type}/{ticker}/{entry_date}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)
    return out_path


def review_with_cli(chart_path, prompt):
    """Send chart to Claude CLI for review. Returns 'APPROVE' or 'REJECT'."""
    cmd = [
        "claude", "-p", prompt, "--image", chart_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout.strip().upper()
        # Extract verdict from output
        if "APPROVE" in output:
            return "APPROVE"
        elif "REJECT" in output:
            return "REJECT"
        else:
            print(f"    Unclear CLI response: {result.stdout.strip()[:200]}")
            return "REJECT"  # Default to reject if unclear
    except subprocess.TimeoutExpired:
        print(f"    CLI timeout")
        return "REJECT"
    except FileNotFoundError:
        print(f"    ERROR: 'claude' CLI not found in PATH")
        sys.exit(1)


def approve_pending(setup_type, pending_id):
    """Approve a pending example via API."""
    r = requests.post(f"{API_BASE}/api/pending/{setup_type}/{pending_id}/approve", timeout=30)
    r.raise_for_status()
    return r.json()


def reject_pending(setup_type, pending_id):
    """Reject a pending example via API."""
    r = requests.post(f"{API_BASE}/api/pending/{setup_type}/{pending_id}/reject", timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser(description="AI Sample Review via Claude CLI")
    parser.add_argument("--setup", required=True, help="Setup type (e.g., dtss)")
    args = parser.parse_args()

    setup = args.setup
    prompt = REVIEW_PROMPTS.get(setup)
    if not prompt:
        print(f"  No review prompt defined for setup: {setup}")
        sys.exit(1)

    print(f"\n  AI Sample Review — {setup.upper()}")
    print(f"  {'='*50}")

    pending = get_pending(setup)
    if not pending:
        print(f"  No pending examples to review.")
        return

    print(f"  {len(pending)} pending examples to review\n")

    approved = 0
    rejected = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        for p in pending:
            ticker = p["ticker"]
            entry_date = p["entry_date"]
            pending_id = p["id"]

            print(f"  Reviewing {ticker} {entry_date}...", end=" ", flush=True)

            # Download chart
            chart_path = os.path.join(tmpdir, f"{ticker}_{entry_date}.png")
            try:
                download_chart(setup, ticker, entry_date, chart_path)
            except Exception as e:
                print(f"SKIP (chart failed: {e})")
                continue

            # Send to Claude CLI
            verdict = review_with_cli(chart_path, prompt)

            if verdict == "APPROVE":
                approve_pending(setup, pending_id)
                approved += 1
                print(f"✓ APPROVED")
            else:
                reject_pending(setup, pending_id)
                rejected += 1
                print(f"✗ REJECTED")

            # Small delay to not hammer the API
            time.sleep(1)

    print(f"\n  {'='*50}")
    print(f"  Done: {approved} approved, {rejected} rejected")


if __name__ == "__main__":
    main()
