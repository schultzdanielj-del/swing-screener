"""
Pyramidal Grinder — Nested time-horizon expression discovery.

MULTI-PASS MODE (default):
  Runs 3 sequential passes to prevent HTF expressions from crowding out daily:
    Pass 1 (Daily+LSP+Algo): Full pyramid (D1→5yr) with 4,141 daily+LSP+algo expressions
    Pass 2 (Weekly):     1mo→5yr tiers with 4,017 weekly expressions on top
    Pass 3 (Monthly):    6mo→5yr tiers with 4,017 monthly expressions on top
  Daily gets first crack at every horizon. Weekly/monthly only add value
  where daily couldn't finish the job.

SINGLE-PASS MODE (--single-pass):
  Legacy mode: all 12,131 expressions in one pass through D1→5yr.

Each tier:
  1. Builds a matrix of ticker-day rows for its window (pre-filtered by locked conditions)
  2. Runs spiderweb beam search scoring by peak signals/day (not total pass rate)
  3. Locks any conditions that reduce peak below threshold
  4. Advances to the next wider window

Tiers:
  D1:  Today (1 bar/ticker) — classic spiderweb, scored by pass count
  T2:  5 trading days        — scored by max(daily_signal_counts)
  T3:  21 trading days (1mo) — scored by max(daily_signal_counts)
  T4:  126 trading days (6mo)— scored by max(daily_signal_counts)
  T5:  252 trading days (1yr)— scored by max(daily_signal_counts)
  T6:  Full history (5yr)    — scored by max(daily_signal_counts)

Constraint: 100% of setup examples ALWAYS pass all conditions (zero false negatives).

Usage:
    # Multi-pass (default):
    python local_runner/pyramid_grinder.py --setup dtss --beam 10000 --depth 100 --peak-target 3

    # Legacy single-pass:
    python local_runner/pyramid_grinder.py --setup dtss --single-pass --beam 10000 --depth 100

Requires:
  - 5-year OHLCV cache (local_runner/cache/universe_ohlcv_5yr.pkl)
  - Expression series cache (local_runner/cache/expr_series/)
  - Example data (via Railway API)
  - Expression library (brute_expressions.py)

"""

import os
import sys
import time
import json
import pickle
import argparse

# Force UTF-8 output on Windows (cp1252 can't handle ≤, ✓, etc.)
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import cpu_count

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from brute_expressions import generate_all
from expr_cache_builder import ExprSeriesCache

API_BASE = "https://web-production-e3025.up.railway.app"

# Tier definitions: (name, n_bars_from_end, description)
# n_bars=0 means "last bar only" (D1 tier uses point-value matrix like current spiderweb)
TIERS = [
    ("D1",   1,    "Today (last bar)"),
    ("1wk",  5,    "5 trading days"),
    ("1mo",  21,   "1 month"),
    ("6mo",  126,  "6 months"),
    ("1yr",  252,  "1 year"),
    ("5yr",  0,    "Full history"),  # 0 = use all available bars
]