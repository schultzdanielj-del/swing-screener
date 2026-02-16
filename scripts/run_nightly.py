"""
Main entry point for nightly screening workflow.

Usage:
    python scripts/run_nightly.py --tickers AAPL,MSFT,NVDA,TSLA
    python scripts/run_nightly.py --file input/tickers.txt
"""
import argparse
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ingest import parse_tickers, load_ticker_file
from src.data import fetch_batch
from src.charts import generate_batch, load_config
from src.batch import create_batches


def main():
    parser = argparse.ArgumentParser(description="Swing Screener \u2014 Nightly Chart Generation")
    parser.add_argument('--tickers', type=str, help='Comma-separated ticker list')
    parser.add_argument('--file', type=str, help='Path to ticker file (one per line)')
    parser.add_argument('--config', type=str, default='config.yaml', help='Config file path')
    args = parser.parse_args()
    
    # Parse tickers
    if args.tickers:
        tickers = parse_tickers(args.tickers)
    elif args.file:
        tickers = load_ticker_file(args.file)
    else:
        print("Error: Provide --tickers or --file")
        sys.exit(1)
    
    if not tickers:
        print("Error: No valid tickers found")
        sys.exit(1)
    
    print(f"\n{'='*50}")
    print(f"  Swing Screener \u2014 Nightly Run")
    print(f"  {len(tickers)} tickers to process")
    print(f"{'='*50}\n")
    
    # Load config
    config = load_config(args.config)
    lookback = config.get('chart', {}).get('lookback_days', 120)
    
    # Step 1: Fetch data
    print("Step 1: Fetching OHLCV data...")
    data = fetch_batch(tickers, lookback_days=lookback)
    
    if not data:
        print("Error: No data fetched for any ticker")
        sys.exit(1)
    
    # Step 2: Generate charts
    print("\nStep 2: Generating D1 charts...")
    output_dir = config.get('output', {}).get('charts_dir', 'output/charts')
    chart_paths = generate_batch(data, output_dir=output_dir, config=config)
    
    # Step 3: Create batches for Claude vision
    print("\nStep 3: Creating batches for vision analysis...")
    batch_size = config.get('batch', {}).get('size', 20)
    batches = create_batches(chart_paths, batch_size=batch_size)
    
    # Summary
    print(f"\n{'='*50}")
    print(f"  Done! {len(chart_paths)} charts ready in {output_dir}/")
    print(f"  {len(batches)} batch(es) ready for Claude vision analysis")
    print(f"{'='*50}\n")


if __name__ == '__main__':
    main()
