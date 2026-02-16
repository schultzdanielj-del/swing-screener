# Swing Screener

Visual pattern screener for swing trade setups. Takes TC2000 scan results and matches D1 charts against a curated setup library using vision-based pattern matching.

## Overview

1. **Ingest** — Paste ticker lists from TC2000 scans
2. **Generate** — Produce clean D1 candlestick charts via yfinance + mplfinance
3. **Match** — Compare charts against curated setup library using Claude vision
4. **Output** — Bucketed results: **Actionable** (ready for entry) and **NMS** (Need More Sideways)

## Quick Start

```bash
pip install -r requirements.txt

# Generate charts for a list of tickers
python scripts/run_nightly.py --tickers AAPL,MSFT,NVDA,TSLA

# Or from a file (one ticker per line)
python scripts/run_nightly.py --file input/tickers.txt
```

## Project Structure

```
swing-screener/
├── config.yaml                  # Chart styling, MA periods, scan config
├── src/
│   ├── ingest.py               # Ticker list parsing and validation
│   ├── data.py                 # yfinance data fetching with caching
│   ├── charts.py               # mplfinance chart generation
│   ├── batch.py                # Organize charts into upload batches
│   └── results.py              # Parse Claude output into structured results
├── setup_library/              # Curated setup examples and descriptions
├── output/
│   ├── charts/                 # Generated chart images
│   └── results/                # Nightly screening results
└── scripts/
    └── run_nightly.py          # Main entry point
```

## Setup Library

Each setup type lives in `setup_library/` with:
- `description.md` — What makes this setup ideal
- `examples/` — Annotated screenshot PNGs of perfect examples
- `discord_commentary.md` — Curated pro trader commentary from Discord
