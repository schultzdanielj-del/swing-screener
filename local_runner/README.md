# Local Runner — THE GRINDER

Desktop agent that runs the spiderweb condition search on your machine.

## Setup (one time)

```bash
cd swing-screener
pip install -r local_runner/requirements.txt
```

## Usage

### Start the agent
```bash
python local_runner/agent.py
```

Leave it running. It polls Railway every 5 seconds for grind jobs.
When you click "Start Grind" in the frontend, the agent picks it up.

First run will:
1. Build OHLCV cache (~3 min, cached for 24h)
2. Generate expressions (~1,300)
3. Compute value matrix (~20-45 min, cached until expressions change)
4. Run spiderweb search (time depends on grind level)

Subsequent runs skip steps 1-3 and go straight to the search.

### Manual run (without agent)
```bash
python local_runner/grinder.py --setup dtss --level 3
```

## Grind Levels

| Level | Name | Beam Width | Depth | Est. Time |
|-------|------|-----------|-------|-----------|
| 1 | Quick scan | 10 | 5 | ~30s |
| 2 | Light grind | 25 | 8 | ~2 min |
| 3 | Medium grind | 50 | 10 | ~10 min |
| 4 | Heavy grind | 100 | 12 | ~30 min |
| 5 | Overnight | 250 | 15 | ~2-8 hours |

## How it works

1. **Expressions**: 1,338 technical measurements (MA slopes, extensions, RSI, volume, patterns)
2. **Matrix**: Compute every expression for every ticker + every example
3. **Thresholds**: For each expression, find the value range where ALL examples pass
4. **Spiderweb**: Search for combinations of conditions that progressively filter the universe
   - Beam search explores multiple branching paths simultaneously
   - Each branch adds one more condition (AND logic)
   - Dead branches pruned when no improvement possible
   - Higher grind level = more branches explored = better combos found

The slider controls compute budget. Further right = deeper search = tighter filter.
