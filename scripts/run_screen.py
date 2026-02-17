"""
Main screening script.

Usage:
    python scripts/run_screen.py --tickers AAPL,MSFT,NVDA
    python scripts/run_screen.py --file input/tickers.txt
    python scripts/run_screen.py --tickers AAPL,MSFT,NVDA --charts-only

Steps:
    1. Parse ticker list
    2. Fetch D1 data via yfinance
    3. Generate individual charts
    4. Build composite grids (20 charts per image)
    5. Generate prompt.txt
    6. Open output folder

Then paste prompt.txt + grid images into Claude chat.
"""
import argparse
import os
import sys
import subprocess
import platform

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ingest import parse_tickers, load_ticker_file
from src.data import fetch_all
from src.charts import generate_batch
from src.composite import build_composites
from src.prompt import save_chat_prompt


def open_folder(path: str):
    """Open a folder in the OS file explorer."""
    path = os.path.abspath(path)
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.run(["open", path])
        else:
            subprocess.run(["xdg-open", path])
    except Exception:
        print(f"  Could not auto-open folder. Navigate to: {path}")


def main():
    parser = argparse.ArgumentParser(description="Swing Screener — generate charts for Claude analysis")
    parser.add_argument("--tickers", type=str, help="Comma-separated ticker list")
    parser.add_argument("--file", type=str, help="Path to ticker list file (one per line)")
    parser.add_argument("--charts-only", action="store_true", help="Skip composite grid generation")
    parser.add_argument("--grids-per", type=int, default=20, help="Charts per composite grid (default: 20)")
    args = parser.parse_args()

    if not args.tickers and not args.file:
        parser.error("Provide --tickers or --file")

    # 1. Parse tickers
    if args.file:
        tickers = load_ticker_file(args.file)
    else:
        tickers = parse_tickers(args.tickers)

    print(f"\n{'='*50}")
    print(f"SWING SCREENER")
    print(f"{'='*50}")
    print(f"  Tickers: {len(tickers)}")
    print(f"  {', '.join(tickers[:20])}{'...' if len(tickers) > 20 else ''}")

    # 2. Fetch data
    print(f"\n--- Fetching D1 data ---")
    data = fetch_all(tickers)
    print(f"  Fetched {len(data)}/{len(tickers)} tickers")

    # 3. Generate individual charts
    print(f"\n--- Generating charts ---")
    chart_paths = generate_batch(data)

    if not chart_paths:
        print("  No charts generated. Exiting.")
        return

    if args.charts_only:
        print(f"\n  Done — charts saved to output/charts/")
        open_folder("output/charts")
        return

    # 4. Build composite grids
    print(f"\n--- Building composite grids ---")
    composites = build_composites(chart_paths, charts_per_grid=args.grids_per)

    # 5. Generate prompt
    print(f"\n--- Generating prompt ---")
    save_chat_prompt()

    # 6. Summary
    print(f"\n{'='*50}")
    print(f"READY TO SCREEN")
    print(f"{'='*50}")
    print(f"  Charts: {len(chart_paths)}")
    print(f"  Grids:  {len(composites)}")
    print(f"")
    print(f"  Next steps:")
    print(f"  1. Open output/prompt.txt and copy the contents")
    print(f"  2. Paste into a new Claude chat")
    print(f"  3. Attach the {len(composites)} grid image(s) from output/composites/")
    print(f"  4. Send — Claude will classify every chart")
    print(f"{'='*50}")

    open_folder("output/composites")


if __name__ == "__main__":
    main()
