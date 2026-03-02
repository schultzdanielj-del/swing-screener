"""
Upload signal filter + exit grind results to Railway for the vetting UI.

Usage:
    python scripts/upload_vetting_data.py --setup dtss

No git required. Just run after signal_filter.py completes.
"""

import argparse
import json
import os
import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAILWAY_URL = "https://web-production-e3025.up.railway.app"


def upload_file(url, path, label):
    if not os.path.exists(path):
        print(f"  SKIP {label}: {path} not found")
        return False
    with open(path) as f:
        data = json.load(f)
    print(f"  Uploading {label} ({os.path.getsize(path) / 1024:.0f} KB)...")
    r = requests.post(url, json=data, timeout=60)
    r.raise_for_status()
    result = r.json()
    print(f"  OK: {result}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", default="dtss")
    args = parser.parse_args()

    setup = args.setup
    print(f"\nUploading vetting data for {setup.upper()} to Railway...\n")

    filtered_path = os.path.join(REPO_ROOT, "data", "signal_filter", f"filtered_{setup}.json")
    upload_file(
        f"{RAILWAY_URL}/api/vetting/{setup}/upload-signals",
        filtered_path,
        "filtered signals"
    )

    exit_path = os.path.join(REPO_ROOT, "data", "signal_exit_grind", f"signal_exit_{setup}.json")
    upload_file(
        f"{RAILWAY_URL}/api/vetting/{setup}/upload-exit",
        exit_path,
        "signal exit grind"
    )

    print(f"\nDone. Open: {RAILWAY_URL}/vetting.html?setup={setup}\n")


if __name__ == "__main__":
    main()
