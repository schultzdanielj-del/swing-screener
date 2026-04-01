"""
Expression Dependency Audit
===========================
For every expression in the library, trace exactly what inputs are needed
to compute its value at bar i, given:
  - The previous bar's expression values (row i-1 of the cache)
  - Today's OHLCV candle
  - Any additional state

Output: a JSON report classifying every expression by:
  1. What "state" it needs beyond (prev_row + today_ohlcv)
  2. How far back it needs to look (in bars, at what resolution)
  3. What external data sources it needs (daily pickle, HTF pickles)
"""

import sys, os, json, re
from collections import defaultdict

# Load from repo files we already downloaded
REPO = "/home/claude/audit/repo_files"
os.makedirs(REPO, exist_ok=True)

def fetch_file(path):
    """Fetch a file from the repo."""
    import urllib.request
    token = "GITHUB_TOKEN_HERE"
    url = f"https://api.github.com/repos/schultzdanielj-del/swing-screener/contents/{path}?ref=v2"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3.raw"
    })
    resp = urllib.request.urlopen(req)
    data = resp.read().decode()
    local = os.path.join(REPO, os.path.basename(path))
    with open(local, "w") as f:
        f.write(data)
    return local

# Fetch needed files
print("Fetching files...")
brute_path = fetch_file("local_runner/brute_expressions.py")
print(f"  brute_expressions.py: {brute_path}")

# Load expression library
sys.path.insert(0, os.path.dirname(brute_path))
# brute_expressions needs a cache dir
os.makedirs(os.path.join(os.path.dirname(brute_path), "cache"), exist_ok=True)

# Parse brute_expressions to get all ops and their params
# We'll exec the generate_all function
spec = {}
with open(brute_path) as f:
    code = f.read()

# Execute to get actual expression list
exec_globals = {"__builtins__": __builtins__, "json": json, "os": os, "__file__": brute_path}
exec(code, exec_globals)
generate_all = exec_globals["generate_all"]
expressions = generate_all()

print(f"\nTotal expressions: {len(expressions)}")

# ============================================================
# CLASSIFY EVERY EXPRESSION
# ============================================================

# For each expression, determine:
# - op type
# - what intermediate indicators it needs
# - what lookback those indicators need
# - whether that lookback is daily or HTF resolution
# - what can be carried forward vs what needs historical access

# Indicator dependency map: what raw computation each indicator needs
INDICATOR_DEPS = {
    # SMA: needs sum of last N values. 
    # Forward: cumsum[i] - cumsum[i-N]. Needs cumsum state + value at i-N.
    "avgc": {"type": "sma", "input": "close", "lookback": "period", "resolution": "same"},
    "avgv": {"type": "sma", "input": "volume", "lookback": "period", "resolution": "same"},
    
    # EMA: needs previous EMA value only
    "xavgc": {"type": "ema", "input": "close", "lookback": 1, "resolution": "same"},
    
    # ATR: SMA of true_range(14)
    "atr14": {"type": "sma", "input": "true_range", "lookback": 14, "resolution": "same"},
    "adr14": {"type": "sma", "input": "hl_range", "lookback": 14, "resolution": "same"},
    
    # RSI: Wilder smoothing of avg_gain/avg_loss
    "rsi": {"type": "wilder_ema", "input": "gain_loss", "lookback": 1, "resolution": "same",
            "note": "needs avg_gain[i-1] and avg_loss[i-1] as state"},
    
    # ADX: EMA chain (DM+ → smoothed DM+ → DI+ → DX → smoothed DX)
    "adx": {"type": "ema_chain", "input": "dm_plus_minus", "lookback": 1, "resolution": "same"},
    "diplus": {"type": "ema_chain", "input": "dm_plus", "lookback": 1, "resolution": "same"},
    "diminus": {"type": "ema_chain", "input": "dm_minus", "lookback": 1, "resolution": "same"},
    
    # Stochastic: 3-bar SMA of raw_k. raw_k needs rolling max/min of H and L.
    "stoch": {"type": "sma_of_stoch_raw", "input": "raw_k", "lookback": "period+2",
              "resolution": "same", "note": "raw_k needs rolling max(H,period) and min(L,period)"},
    
    # CCI: (tp - SMA(tp)) / (0.015 * mean_deviation). Mean deviation needs full window.
    "cci": {"type": "window_stat", "input": "typical_price", "lookback": "period", "resolution": "same",
            "note": "mean absolute deviation requires full window of TP values"},
    
    # BOP: SMA of raw BOP
    "bop": {"type": "sma", "input": "bop_raw", "lookback": "period", "resolution": "same"},
    
    # OBV: cumulative
    "obv": {"type": "cumulative", "input": "signed_volume", "lookback": 1, "resolution": "same"},
    
    # MACD: difference of two EMAs (already tracked)
    "macd": {"type": "ema_diff", "lookback": 1, "resolution": "same"},
    
    # Bollinger: SMA ± 2*stddev. Stddev needs window.
    "bollinger": {"type": "window_stat", "input": "close", "lookback": "period",
                  "resolution": "same", "note": "stddev requires full window or cumsum_c2 trick"},
    
    # Aroon: position of max/min in window
    "aroon": {"type": "window_argmax", "input": "high_low", "lookback": "period",
              "resolution": "same", "note": "needs to know WHERE in window the max/min is"},
    
    # CMF: rolling sum of MFV / rolling sum of volume
    "cmf": {"type": "sma_ratio", "input": "mfv_volume", "lookback": "period", "resolution": "same"},
    
    # Kaufman: |close - close[i-p]| / sum(|close[i] - close[i-1]| for last p bars)
    "kaufman": {"type": "window_stat", "input": "close_diffs", "lookback": "period",
                "resolution": "same", "note": "needs close[i-period] and cumsum of abs diffs"},
    
    # Rolling max/min
    "maxh": {"type": "rolling_max", "input": "high", "lookback": "period", "resolution": "same"},
    "minl": {"type": "rolling_min", "input": "low", "lookback": "period", "resolution": "same"},
    "maxc": {"type": "rolling_max", "input": "close", "lookback": "period", "resolution": "same"},
}

# Classify each expression
categories = defaultdict(list)
max_lookbacks = defaultdict(int)  # resolution -> max bars
issues = []

for i, expr in enumerate(expressions):
    comp = expr["compute"]
    op = comp.get("op", "")
    name = expr["name"]
    
    info = {
        "name": name,
        "op": op,
        "params": {k: v for k, v in comp.items() if k != "op"},
        "lookback_bars": 0,
        "lookback_resolution": "daily",
        "needs_state": [],
        "needs_historical_access": False,
        "historical_access_what": None,
        "external_data": [],
    }
    
    if op == "precomputed":
        source = comp.get("source")
        tf = comp.get("timeframe")
        if source == "lsp":
            info["needs_state"].append("lsp_pivot_state")
            info["external_data"].append("daily_ohlcv_for_pivot_detection")
            info["lookback_bars"] = 40  # max pivot window
            info["notes"] = "pivot detection needs N bars each side"
        elif source == "algo":
            info["needs_state"].append("algo_line_state")
            info["external_data"].append("daily_ohlcv_for_trendline_detection")
        elif source == "htf":
            base_comp = comp.get("base_compute", {})
            base_op = base_comp.get("op", "")
            info["lookback_resolution"] = "htf_" + (tf or "?")
            
            # HTF expressions need the closed HTF series intermediates
            # PLUS today's partial candle
            info["external_data"].append(f"htf_{tf}_ohlcv_pickle")
            info["needs_state"].append(f"htf_{tf}_partial_candle")
            info["needs_state"].append(f"htf_{tf}_closed_intermediates")
            
            # The base_comp determines what indicator is needed at HTF resolution
            # and therefore what lookback AT HTF RESOLUTION
            bp = base_comp.get("period", 0)
            blb = base_comp.get("lookback", 0)
            boff = base_comp.get("offset", 0)
            bmaxhp = base_comp.get("maxh_period", 0)
            bminlp = base_comp.get("minl_period", 0)
            
            htf_lookback = max(bp, blb, boff, bmaxhp, bminlp)
            info["lookback_bars"] = htf_lookback
            info["notes"] = f"HTF {tf} base_op={base_op}, needs {htf_lookback} HTF bars"
            
            # Track max HTF lookback
            res_key = f"htf_{tf}"
            if htf_lookback > max_lookbacks[res_key]:
                max_lookbacks[res_key] = htf_lookback
            
            # Boolean agg at HTF resolution
            if base_op in ("count_true", "since_true", "true_in_row"):
                # The boolean condition at HTF res needs its own indicator
                cond = base_comp.get("condition", "")
                info["notes"] += f", bool condition={cond}"
            
            # on_series at HTF resolution
            if base_op in ("on_series", "on_series_bool_agg"):
                inner = base_comp.get("inner_op", {})
                inner_lb = inner.get("lookback", inner.get("period", 0))
                info["lookback_bars"] = max(info["lookback_bars"], inner_lb)
                info["notes"] += f", inner_op={inner.get('op','?')} lookback={inner_lb}"
        
        categories[f"precomputed_{source}_{tf or 'daily'}"].append(info)
        continue
    
    # Daily expressions
    # Determine lookback needed
    period = comp.get("period", 0)
    lookback = comp.get("lookback", 0)
    offset = comp.get("offset", 0)
    maxh_p = comp.get("maxh_period", 0)
    minl_p = comp.get("minl_period", 0)
    max_lb = comp.get("max_lookback", 0)
    roc_p = comp.get("roc_period", 0)
    
    if op in ("extension", "high_vs_ma", "low_vs_ma", "ext_adr_multiples"):
        # Needs MA value at current bar + normalizer
        ma = comp.get("ma", "")
        if ma.startswith("avgc"):
            p = int(ma[4:])
            info["lookback_bars"] = p  # SMA needs cumsum[i-p]
            info["needs_state"].append(f"cumsum_close")
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"cumsum_close at i-{p}"
        elif ma.startswith("xavgc"):
            info["lookback_bars"] = 1  # EMA only needs prev value
            info["needs_state"].append(f"ema_{ma}")
        norm = comp.get("normalizer", "")
        if norm in ("atr14", "adr14"):
            info["needs_state"].append(f"cumsum_for_{norm}")
            info["lookback_bars"] = max(info["lookback_bars"], 14)
    
    elif op == "ma_slope":
        ma = comp.get("ma", "")
        info["lookback_bars"] = offset  # need MA value at i-offset
        if ma.startswith("avgc"):
            p = int(ma[4:])
            info["lookback_bars"] = max(p, offset + p)  # need cumsum far back
            info["needs_state"].append("cumsum_close")
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"cumsum_close at i-{p} and i-{offset}-{p}"
        elif ma.startswith("xavgc"):
            info["lookback_bars"] = offset  # need EMA at i-offset from cache
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"EMA value at i-{offset} from cache"
    
    elif op == "ma_spread":
        fast_ma = comp.get("ma_fast", "")
        slow_ma = comp.get("ma_slow", "")
        max_p = 0
        for ma in [fast_ma, slow_ma]:
            if ma.startswith("avgc"):
                max_p = max(max_p, int(ma[4:]))
            elif ma.startswith("xavgc"):
                pass  # EMA, lookback 1
        info["lookback_bars"] = max_p
        if max_p > 1:
            info["needs_state"].append("cumsum_close")
            info["needs_historical_access"] = True
    
    elif op in ("distance_to_maxh", "ratio_c_maxh"):
        info["lookback_bars"] = maxh_p  # rolling max needs window
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"high values in window of {maxh_p} bars"
    
    elif op in ("distance_to_minl", "ratio_c_minl"):
        info["lookback_bars"] = minl_p
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"low values in window of {minl_p} bars"
    
    elif op in ("rsi", "rsi_slope"):
        info["lookback_bars"] = max(1, offset)
        info["needs_state"].append(f"rsi_avg_gain_{period}")
        info["needs_state"].append(f"rsi_avg_loss_{period}")
        if offset > 0:
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"RSI value at i-{offset}"
    
    elif op in ("adx", "adx_slope", "di_spread"):
        info["lookback_bars"] = max(1, offset)
        info["needs_state"].append(f"adx_ema_chain_{period}")
        if offset > 0:
            info["needs_historical_access"] = True
    
    elif op == "stochastic":
        info["lookback_bars"] = period + 2  # raw_k needs rolling max/min, then 3-bar SMA
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"H/L values in window of {period} + 2 raw_k values"
    
    elif op == "cci":
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"typical_price values in window of {period} for mean deviation"
    
    elif op == "percentile_rank":
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
        src = comp.get("source", "close")
        info["historical_access_what"] = f"last {period} values of {src}"
    
    elif op == "roc_percentile_rank":
        total_lb = roc_p + lookback
        info["lookback_bars"] = total_lb
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"last {lookback} ROC values (each needs close[i-{roc_p}])"
    
    elif op == "extension_peak_ratio":
        info["lookback_bars"] = lookback
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"rolling_max of extension over {lookback} bars"
    
    elif op == "extension_ceiling_ratio":
        info["lookback_bars"] = lookback
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"rolling_max of norm_extension over {lookback} bars"
    
    elif op == "bollinger_bandwidth_rank":
        info["lookback_bars"] = lookback
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"rolling min/max of bandwidth over {lookback} bars"
    
    elif op in ("roc", "roc_delta", "roc_acceleration"):
        info["lookback_bars"] = max(period, period + comp.get("compare_offset", 0))
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"close values at i-{period}"
    
    elif op in ("count_true", "since_true", "true_in_row"):
        info["lookback_bars"] = period
        if op == "count_true":
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"boolean condition value at i-{period} (dropping off window)"
        elif op == "since_true":
            info["lookback_bars"] = 1  # just prev value + current bool
        elif op == "true_in_row":
            info["lookback_bars"] = 1  # just prev value + current bool
    
    elif op in ("on_series", "on_series_bool_agg"):
        inner = comp.get("inner_op", comp.get("bool_op", {}))
        inner_lb = inner.get("lookback", inner.get("period", 0))
        info["lookback_bars"] = inner_lb
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"extension series values in window of {inner_lb}"
        if op == "on_series_bool_agg":
            agg_period = comp.get("agg_period", 0)
            info["lookback_bars"] = max(info["lookback_bars"], agg_period)
    
    elif op == "bars_since_ma_cross":
        info["lookback_bars"] = 1  # increment prev value or reset
        info["needs_state"].append("prev_cross_direction")
    
    elif op in ("swing_high_count", "swing_low_count",
                "higher_high_count", "lower_high_count",
                "higher_low_count", "lower_low_count"):
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"swing detection in window of {period} bars"
    
    elif op in ("range_position", "range_width", "pullback", "retrace_high", "retrace_low",
                "retracement_level", "channel_slope"):
        p = period if period else comp.get("period", 0)
        info["lookback_bars"] = p
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"rolling max/min of H/L over {p} bars"
    
    elif op in ("ma_undercut_depth",):
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
    
    elif op in ("gap_size", "gap_count", "inside_bar_count", "outside_bar_count",
                "nr_ratio", "avg_candle_body_ratio", "close_position_in_bar",
                "close_vs_open_ratio", "range_contraction_ratio"):
        info["lookback_bars"] = max(period, 1)
        if period > 1:
            info["needs_historical_access"] = True
    
    elif op in ("candle_range_ratio", "body_range_ratio", "upper_wick_ratio", "lower_wick_ratio"):
        info["lookback_bars"] = 0  # pure today's candle + ATR
    
    elif op == "volume_ratio":
        info["lookback_bars"] = comp.get("avg_period", 20)
        info["needs_state"].append("cumsum_volume")
    
    elif op == "obv_slope":
        info["lookback_bars"] = max(offset, 1)
        info["needs_state"].append("obv")
        if offset > 1:
            info["needs_historical_access"] = True
            info["historical_access_what"] = f"OBV value at i-{offset}"
    
    elif op in ("bop",):
        info["lookback_bars"] = period
        info["needs_state"].append("cumsum_bop_raw")
    
    elif op in ("cmf", "cmf_slope"):
        info["lookback_bars"] = max(period, offset)
        info["needs_state"].append("cumsum_mfv")
        info["needs_state"].append("cumsum_volume")
        if offset > 0:
            info["needs_historical_access"] = True
    
    elif op in ("macd_histogram", "macd_histogram_slope", "macd_line_norm"):
        info["lookback_bars"] = max(1, offset)
        info["needs_state"].append("macd_signal_ema")
        if offset > 0:
            info["needs_historical_access"] = True
    
    elif op in ("bollinger_pctb", "bollinger_bandwidth"):
        info["lookback_bars"] = period
        info["needs_state"].append("cumsum_close")
        info["needs_state"].append("cumsum_c2")
        info["needs_historical_access"] = True
    
    elif op in ("aroon_up_val", "aroon_down_val", "aroon_oscillator"):
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"H/L values in window of {period} for argmax/argmin"
    
    elif op == "kaufman_efficiency_ratio":
        info["lookback_bars"] = period
        info["needs_historical_access"] = True
        info["historical_access_what"] = f"close[i-{period}] and cumsum_abs_diff"
    
    elif op == "atr_ratio":
        info["lookback_bars"] = max(offset, 14)
        if offset > 1:
            info["needs_historical_access"] = True
    
    elif op == "slope_ratio":
        info["lookback_bars"] = offset
        if offset > 0:
            info["needs_historical_access"] = True
    
    elif op in ("spread_slope",):
        info["lookback_bars"] = offset
        if offset > 0:
            info["needs_historical_access"] = True
    
    elif op == "ma_stack_score":
        info["lookback_bars"] = 200  # compares multiple MAs
        info["needs_state"].extend(["all_ma_values"])
    
    elif op in ("high_volume_bar_pct", "cumulative_rvol", "rvol_continuous",
                "up_volume_ratio", "volume_price_divergence", "vwap_distance",
                "vwap_slope", "unfilled_gap_up_count", "smoothed_ma", "slope",
                "ma_cross_count", "ma_cross", "floor_ratio", "peak_ratio",
                "ceiling_ratio", "trendline_deviation", "channel_position"):
        # Various ops with period-based lookback
        lb = max(period, lookback, offset, 1)
        info["lookback_bars"] = lb
        if lb > 1:
            info["needs_historical_access"] = True
    
    else:
        info["notes"] = f"UNHANDLED OP: {op}"
        issues.append(f"Unhandled op: {op} in expression {name}")
    
    # Track max daily lookback
    if info["lookback_bars"] > max_lookbacks["daily"]:
        max_lookbacks["daily"] = info["lookback_bars"]
    
    categories[op].append(info)

# ============================================================
# SUMMARY REPORT
# ============================================================

print("\n" + "=" * 70)
print("EXPRESSION DEPENDENCY AUDIT")
print("=" * 70)

print(f"\nTotal expressions: {len(expressions)}")
print(f"Unique ops: {len(categories)}")

print(f"\n--- MAX LOOKBACK BY RESOLUTION ---")
for res, lb in sorted(max_lookbacks.items()):
    print(f"  {res}: {lb} bars")

# Count expressions by lookback requirement
lb_buckets = defaultdict(int)
need_hist = 0
no_hist = 0
for op_name, expr_list in categories.items():
    for info in expr_list:
        lb = info["lookback_bars"]
        if lb <= 1:
            lb_buckets["0-1 (state only)"] += 1
        elif lb <= 20:
            lb_buckets["2-20"] += 1
        elif lb <= 50:
            lb_buckets["21-50"] += 1
        elif lb <= 126:
            lb_buckets["51-126"] += 1
        elif lb <= 252:
            lb_buckets["127-252"] += 1
        elif lb <= 504:
            lb_buckets["253-504"] += 1
        else:
            lb_buckets["505+"] += 1
        
        if info["needs_historical_access"]:
            need_hist += 1
        else:
            no_hist += 1

print(f"\n--- EXPRESSIONS BY LOOKBACK DEPTH ---")
for bucket in ["0-1 (state only)", "2-20", "21-50", "51-126", "127-252", "253-504", "505+"]:
    print(f"  {bucket:20s}: {lb_buckets.get(bucket, 0):,}")

print(f"\n--- HISTORICAL ACCESS ---")
print(f"  Need historical data access: {need_hist:,}")
print(f"  State only (no history):     {no_hist:,}")

# What the historical access is for
hist_reasons = defaultdict(int)
for op_name, expr_list in categories.items():
    for info in expr_list:
        if info["needs_historical_access"] and info.get("historical_access_what"):
            # Simplify the reason
            reason = info["historical_access_what"]
            if "rolling_max" in reason or "rolling min" in reason:
                hist_reasons["rolling max/min window"] += 1
            elif "cumsum" in reason:
                hist_reasons["cumulative sum lookback"] += 1
            elif "values in window" in reason or "last" in reason:
                hist_reasons["value window scan"] += 1
            elif "value at i-" in reason:
                hist_reasons["single historical value"] += 1
            elif "close" in reason:
                hist_reasons["close price lookback"] += 1
            elif "swing" in reason:
                hist_reasons["swing detection window"] += 1
            elif "boolean" in reason:
                hist_reasons["boolean history"] += 1
            else:
                hist_reasons[reason] += 1

print(f"\n--- WHY HISTORICAL ACCESS IS NEEDED ---")
for reason, count in sorted(hist_reasons.items(), key=lambda x: -x[1]):
    print(f"  {count:5,}  {reason}")

# HTF analysis
print(f"\n--- HTF EXPRESSIONS ---")
htf_count = 0
htf_max_lb = {"w": 0, "m": 0}
for op_name, expr_list in categories.items():
    for info in expr_list:
        if info.get("lookback_resolution", "").startswith("htf"):
            htf_count += 1
            tf = info["lookback_resolution"].split("_")[1]
            if info["lookback_bars"] > htf_max_lb.get(tf, 0):
                htf_max_lb[tf] = info["lookback_bars"]

print(f"  Total HTF expressions: {htf_count:,}")
for tf, lb in htf_max_lb.items():
    daily_equiv = lb * (5 if tf == "w" else 21)
    print(f"  Max lookback HTF {tf}: {lb} HTF bars = ~{daily_equiv} daily bars")

# External data needs
print(f"\n--- EXTERNAL DATA REQUIREMENTS ---")
ext_data = defaultdict(int)
for op_name, expr_list in categories.items():
    for info in expr_list:
        for ed in info.get("external_data", []):
            ext_data[ed] += 1
for data_src, count in sorted(ext_data.items(), key=lambda x: -x[1]):
    print(f"  {count:5,}  {data_src}")

# State requirements
print(f"\n--- STATE VARIABLES NEEDED ---")
state_vars = defaultdict(int)
for op_name, expr_list in categories.items():
    for info in expr_list:
        for sv in info.get("needs_state", []):
            state_vars[sv] += 1
for sv, count in sorted(state_vars.items(), key=lambda x: -x[1]):
    print(f"  {count:5,}  {sv}")

if issues:
    print(f"\n--- ISSUES ---")
    for issue in issues:
        print(f"  ⚠ {issue}")

# Summary of what the append worker needs
print(f"\n{'=' * 70}")
print("APPEND WORKER INPUT REQUIREMENTS")
print("=" * 70)

print(f"""
1. TODAY'S OHLCV: 1 daily bar (O, H, L, C, V, date)

2. PREVIOUS ROW: Last row of expression cache (15,805 values)
   - Used for: since_true(+1), true_in_row(+1), bars_since_ma_cross(+1)
   - Also provides previous expression values needed by slope/offset ops

3. STATE FILE: ~{len(set(sv for expr_list in categories.values() for info in expr_list for sv in info.get('needs_state', [])))} unique state variables
   - Cumulative sums (close, volume, TR, HL, bop_raw, MFV, abs_diff, TP, C²)
   - EMA states (14 close EMAs + ADX chain + MACD signals)
   - RSI avg_gain/avg_loss per period
   - HTF partial candle state (weekly + monthly)
   - HTF closed intermediate states
   - LSP pivot state
   - Algo line state

4. DAILY LOOKBACK: Up to {max_lookbacks.get('daily', 0)} bars of intermediate values
   - From .lookback file (base .npz tail) or .append file (recent bars)
   - Used for: rolling max/min windows, percentile_rank, CCI mean deviation,
     SMA cumsum lookback, swing detection, aroon argmax, count_true drop-off

5. HTF OHLCV PICKLES: Weekly + monthly (already loaded in nightly pipeline)
   - Used by: {htf_count:,} HTF expressions
   - Max lookback: {htf_max_lb.get('w', 0)} weekly bars, {htf_max_lb.get('m', 0)} monthly bars
   - Needed for: extract_closed_state() to build HTF intermediates
   - This runs on full HTF pickle data (300 weekly / 72 monthly bars)
""")

# Save full report
report = {
    "total_expressions": len(expressions),
    "max_lookbacks": dict(max_lookbacks),
    "lookback_buckets": dict(lb_buckets),
    "need_historical_access": need_hist,
    "state_only": no_hist,
    "htf_count": htf_count,
    "htf_max_lookback": htf_max_lb,
    "state_variables": dict(state_vars),
    "external_data": dict(ext_data),
    "issues": issues,
}

with open("/home/claude/audit/dependency_report.json", "w") as f:
    json.dump(report, f, indent=2)

print(f"\nFull report saved to /home/claude/audit/dependency_report.json")
