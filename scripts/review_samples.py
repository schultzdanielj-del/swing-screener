#!/usr/bin/env python3
"""
AI Sample Review — Uses Claude CLI to vet pending examples.

For each pending example:
1. Downloads chart PNG from Railway API
2. Sends to Claude CLI with setup criteria
3. Stores AI verdict + explanation back in pending_examples table
4. User sees the AI reasoning in the Pending tab and makes final call

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

Respond in this exact format:
VERDICT: APPROVE or REJECT
REASONING: 2-3 sentences explaining what you see on the chart and why you made this call.""",
}


def get_pending(setup_type):
    r = requests.get(f"{API_BASE}/api/pending/{setup_type}", timeout=30)
    r.raise_for_status()
    return r.json().get("pending", [])


def download_chart(setup_type, ticker, entry_date, out_path):
    url = f"{API_BASE}/api/chart/{setup_type}/{ticker}/{entry_date}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(r.content)


def review_with_cli(chart_path, prompt):
    """Send chart to Claude CLI. Returns (verdict, reasoning)."""
    cmd = ["claude", "-p", prompt, "--image", chart_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout.strip()
        verdict = "REJECT"
        reasoning = output

        for line in output.split("\n"):
            up = line.strip().upper()
            if up.startswith("VERDICT:"):
                v = up.replace("VERDICT:", "").strip()
                verdict = "APPROVE" if "APPROVE" in v else "REJECT"
            elif line.strip().upper().startswith("REASONING:"):
                reasoning = line.strip()[len("REASONING:"):].strip()
                idx = output.index(line) + len(line)
                rest = output[idx:].strip()
                if rest:
                    reasoning += " " + rest

        return verdict, reasoning
    except subprocess.TimeoutExpired:
        return "REJECT", "CLI timeout"
    except FileNotFoundError:
        print("  ERROR: 'claude' CLI not found in PATH")
        sys.exit(1)


def store_review(setup_type, pending_id, verdict, reasoning):
    """Store AI verdict + reasoning in pending table."""
    r = requests.post(
        f"{API_BASE}/api/pending/{setup_type}/{pending_id}/review",
        json={"ai_verdict": verdict, "ai_reasoning": reasoning},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", required=True)
    args = parser.parse_args()

    setup = args.setup
    prompt = REVIEW_PROMPTS.get(setup)
    if not prompt:
        print(f"  No review prompt for: {setup}")
        sys.exit(1)

    print(f"\n  AI Sample Review — {setup.upper()}")
    print(f"  {'='*50}")

    pending = get_pending(setup)
    unreviewed = [p for p in pending if p.get("status") == "pending"]

    if not unreviewed:
        print("  No unreviewed pending examples.")
        return

    print(f"  {len(unreviewed)} to review\n")

    n_approve = 0
    n_reject = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        for p in unreviewed:
            ticker, entry_date, pid = p["ticker"], p["entry_date"], p["id"]
            print(f"  {ticker} {entry_date}...", end=" ", flush=True)

            chart_path = os.path.join(tmpdir, f"{ticker}_{entry_date}.png")
            try:
                download_chart(setup, ticker, entry_date, chart_path)
            except Exception as e:
                print(f"SKIP ({e})")
                continue

            verdict, reasoning = review_with_cli(chart_path, prompt)
            store_review(setup, pid, verdict, reasoning)

            tag = "✓" if verdict == "APPROVE" else "✗"
            print(f"{tag} {verdict} — {reasoning[:80]}")
            n_approve += verdict == "APPROVE"
            n_reject += verdict == "REJECT"

            time.sleep(1)

    print(f"\n  {'='*50}")
    print(f"  {n_approve} approved, {n_reject} rejected")
    print(f"  Check Pending tab to review AI reasoning and make final calls.")


if __name__ == "__main__":
    main()
