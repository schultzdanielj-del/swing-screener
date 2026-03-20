"""Wrapper that runs profit_grinder with optimized 2-stage.

Run this instead of profit_grinder.py directly:
    python scripts/run_profit_grinder.py --setup dtss

It imports the optimized 2-stage (outer loop = expressions, extract each
column once) and patches it in before calling main().
"""
import sys, os

# Ensure scripts/ is on path for imports
scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

# Import the optimized 2-stage and patch it into the main module
import profit_grinder
import profit_grinder_2stage

# Replace the unoptimized grind_2stage with the optimized version
profit_grinder.grind_2stage = profit_grinder_2stage.grind_2stage

# Run
profit_grinder.main()
