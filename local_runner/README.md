# Local Runner — THE GRINDER

Brute force expression discovery engine. Runs on your desktop for maximum compute power.

## Setup (one time)

```bash
cd swing-screener
pip install -r local_runner/requirements.txt
```

## Usage

### 1. Build OHLCV cache (first run or daily refresh)
```bash
python local_runner/cache_builder.py         # builds if stale (>24h)
python local_runner/cache_builder.py --force  # force rebuild
```
Pulls all 4,167 tradable tickers from Railway DB. Takes ~5-10 min.
Saves to `local_runner/cache/universe_ohlcv.pkl`

### 2. Generate brute force expressions
```bash
python local_runner/brute_expressions.py
```
Generates ~1,300+ expressions covering every parameter combination.
Saves to `local_runner/cache/brute_expressions.json`

### 3. Run THE GRINDER

**Test mode** (quick — 25 expressions, 50 tickers):
```bash
python local_runner/grinder.py --test
```

**Full run** (all expressions, all tickers):
```bash
python local_runner/grinder.py --setup dtss
```

**Partial run** (all expressions, limited tickers):
```bash
python local_runner/grinder.py --setup dtss --max-universe 500
```

### Output

Results saved to:
- `local_runner/cache/grinder_results_dtss.json` — scores + rankings
- `local_runner/cache/grinder_matrix_dtss.pkl` — full value matrices

Top results auto-uploaded to Railway API for dashboard display.

## What it does

For every expression (1,300+) × every ticker (4,167):
1. Computes the expression value at the target date
2. Scores how well it separates setup examples from the general universe
3. Ranks by combined score: separation × consistency × selectivity

**Score components:**
- **Separation**: How far apart are example values from universe values? (Cohen's d)
- **Consistency**: How tightly do examples cluster? (low variance = good)
- **Selectivity**: What % of universe falls in the example range? (low = good)

The top-scoring expressions are the ones that mathematically describe
what makes this setup unique. Those become scan conditions.

## Estimated times (desktop)

| Run type | Expressions | Tickers | Time |
|----------|------------|---------|------|
| Test | 25 | 50 | ~30s |
| Partial | 1,338 | 500 | ~5 min |
| Full | 1,338 | 4,167 | ~45 min |
