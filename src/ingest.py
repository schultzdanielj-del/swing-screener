"""
Ticker list parsing and validation.
Handles input from TC2000 scan exports and manual ticker lists.
"""
import re
from typing import List


def parse_tickers(raw_input: str) -> List[str]:
    """
    Parse tickers from various input formats:
    - Comma-separated: "AAPL, MSFT, NVDA"
    - Newline-separated (TC2000 export)
    - Space-separated
    - Mixed with noise characters
    
    Returns deduplicated, uppercase ticker list.
    """
    # Split on common delimiters
    tickers = re.split(r'[,\s\n\r\t]+', raw_input.strip())
    
    # Clean and validate each ticker
    cleaned = []
    for t in tickers:
        t = t.strip().upper()
        # Basic ticker validation: 1-5 alpha chars, optional dot for BRK.B style
        if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', t):
            cleaned.append(t)
    
    # Deduplicate while preserving order
    seen = set()
    result = []
    for t in cleaned:
        if t not in seen:
            seen.add(t)
            result.append(t)
    
    return result


def load_ticker_file(filepath: str) -> List[str]:
    """Load tickers from a text file (one per line or comma-separated)."""
    with open(filepath, 'r') as f:
        return parse_tickers(f.read())
